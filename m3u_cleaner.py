#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
m3u_cleaner.py — отдельный чистильщик M3U-листов и URL.

Особенности:
  • Стриминг: читает/пишет построчно, не держит лист в памяти
  • СОХРАНЯЕТ #EXTVLCOPT (http-user-agent и др.) у каждого канала
  • СОХРАНЯЕТ шапку #EXTM3U url-tvg="..." (для EPG в плеере)
  • Дедуп по URL глобальный (между всеми источниками)
  • Фильтры: пустые URL, схемы, приватные IP, multicast,
    сокращатели, заблокированные домены, мусорные имена

Запуск:
    python m3u_cleaner.py
    python m3u_cleaner.py --strip-trackers
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("❌ pip install requests")
    sys.exit(1)

# ---------- Константы ----------
DEFAULT_UA = "WINK/RT_(Android_TV/11)_WinkPlayer_AppleWebKit/537.36"
DEFAULT_TIMEOUT = 30
CHUNK = 8192
SHORT_PATTERN_LEN = 5  # слова такой длины и короче матчим по границе слова

PRIVATE_PREFIXES = ("127.", "10.", "192.168.", "169.254.", "0.")
SHORTENERS = ("bit.ly", "tinyurl.com", "clck.ru", "adf.ly", "bitly.com",
              "tiny.cc", "rb.gy", "surl.li", "goo.gl", "is.gd", "ow.ly")
DEFAULT_DOMAIN_BLOCK = ("cinerama.uz",)
DEFAULT_NAME_EXCLUDE = (
    # детские
    "детск", "дети", "мульт", "мультик", "карусель", "малыш", "малютк", "сказк",
    "kids", "baby", "cartoon", "disney", "nickelodeon", "boomerang", "cartoonito",
    "gulli", "tiji", "jimjam", "babytv", "da vinci", "carousel",
    "mult", "ani", "multilandia", "supergeroi", "v gostyakh u skazki",
    # магазины / мода
    "магазин", "шопинг", "телегазета", "телемагазин", "распродаж",
    "ювелирочка", "ювелир",
    "shop", "shopping", "shop24", "teleshop", "teleshopping",
    "fashion", "fashion&", "fashion-", "fashion tv", "fashiontv",
    "style", "lifestyle", "beauty", "glamour", "leomax", "home shopping",
    "qvc", "hsn", "vitrina", "best sell", "bestseller", "top shop",
)
TRACKER_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                  "utm_content", "fbclid", "gclid", "yclid", "ref", "referrer"}
ATTR_RE = re.compile(r'([A-Za-z0-9\-_]+)\s*=\s*"([^"]*)"')
IPV4_172_RE = re.compile(r"^172\.(\d+)\.")
VALID_SCHEMES = ("http://", "https://")
MULTICAST_SCHEMES = ("udp://", "rtp://", "rtsp://", "igmp://")


def log(m: str) -> None:
    print(m, flush=True)


# ---------- Парсинг ----------
def split_extinf(line: str):
    """EXTINF с корректной обработкой запятых внутри кавычек."""
    in_q = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ',' and not in_q:
            return line[:i], line[i + 1:].strip()
    return line, ""


def iter_m3u(lines):
    """
    Ленивый парсер.
    Yield: (attrs_dict, name, directives_list, url)
    directives — все #EXT... строки между #EXTINF и URL (#EXTVLCOPT и т.п.)
    """
    attrs: dict = {}
    name = ""
    directives: list = []
    in_entry = False
    for raw in lines:
        if raw is None:
            continue
        line = raw.rstrip("\r\n").rstrip()
        if not line:
            continue
        stripped = line.lstrip()
        if stripped.startswith("#EXTM3U"):
            continue
        if stripped.startswith("#EXTINF"):
            attrs_part, name_val = split_extinf(stripped)
            attrs = dict(ATTR_RE.findall(attrs_part))
            name = name_val
            directives = []
            in_entry = True
            continue
        if stripped.startswith("#"):
            if in_entry:
                directives.append(stripped)
            continue
        # URL строка
        if in_entry:
            yield attrs, name, directives, stripped
            attrs, name, directives, in_entry = {}, "", [], False
    return


# ---------- Проверки ----------
def is_private_ip(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if not host:
        return False
    if host.startswith(PRIVATE_PREFIXES):
        return True
    m = IPV4_172_RE.match(host)
    if m and 16 <= int(m.group(1)) <= 31:
        return True
    if host.count(":") >= 2:
        return True  # IPv6
    return False


def host_match(url: str, domains) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == d or h.endswith("." + d) for d in domains)


def is_multicast_url(url: str) -> bool:
    """UDP/RTP multicast или его HTTP-обёртка."""
    u = url.lower()
    if u.startswith(MULTICAST_SCHEMES):
        return True
    p = urlparse(url).path.lower()
    return "/udp/" in p or "/rtp/" in p or "/multicast/" in p


def strip_tracking(url: str) -> str:
    try:
        p = urlparse(url)
        if not p.query:
            return url
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in TRACKER_PARAMS]
        return urlunparse(p._replace(query=urlencode(q)))
    except Exception:
        return url


def name_has_banned(name: str, banned) -> bool:
    """
    Длинные паттерны (>5 симв) — подстрока.
    Короткие (ani, mult, shop…) — только как отдельное слово,
    чтобы 'Ani' не ловил 'Animal Planet', а 'Shop' — 'Shopalot'.
    """
    low = name.lower()
    for b in banned:
        if len(b) <= SHORT_PATTERN_LEN:
            if re.search(rf"(?<![a-zа-я0-9]){re.escape(b)}(?![a-zа-я0-9])", low):
                return True
        else:
            if b in low:
                return True
    return False


def reject_reason(url: str, name: str, flt) -> str | None:
    if not url:
        return "empty_url"
    if not url.lower().startswith(VALID_SCHEMES):
        return "bad_scheme"
    if is_multicast_url(url):
        return "multicast"
    if is_private_ip(url):
        return "private_ip"
    if host_match(url, flt["shortener_blocklist"]):
        return "shortener"
    if host_match(url, flt["domain_blocklist"]):
        return "blocked_domain"
    if name and name_has_banned(name, flt["name_exclude"]):
        return "banned_name"
    return None


# ---------- Сеть ----------
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=1,
                  status_forcelist=[429, 500, 502, 503, 504])
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def stream_lines(session, url: str, timeout: int):
    r = session.get(url, timeout=timeout, stream=True,
                    headers={"User-Agent": DEFAULT_UA})
    r.raise_for_status()
    for raw in r.iter_lines(chunk_size=CHUNK, decode_unicode=False):
        if raw is None:
            continue
        try:
            yield raw.decode("utf-8-sig", errors="replace")
        except Exception:
            yield raw.decode("utf-8", errors="replace")


def local_lines(path: str):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            yield line


# ---------- Конфиги ----------
def _as_lower_list(v):
    if isinstance(v, str):
        return [s.strip().lower() for s in v.split(",") if s.strip()]
    if isinstance(v, list):
        return [str(s).strip().lower() for s in v if str(s).strip()]
    return []


def load_filters(path: str):
    flt = {
        "name_exclude": list(DEFAULT_NAME_EXCLUDE),
        "domain_blocklist": list(DEFAULT_DOMAIN_BLOCK),
        "shortener_blocklist": list(SHORTENERS),
    }
    p = Path(path)
    if not p.exists():
        return flt
    try:
        fj = json.loads(p.read_text(encoding="utf-8-sig"))
        for key in ("name_exclude", "domain_blocklist", "shortener_blocklist"):
            v = _as_lower_list(fj.get(key))
            if v:
                flt[key] = v
    except Exception as e:
        log(f"⚠️  filters.json: {e} — беру дефолты")
    return flt


def load_sources(path: str):
    p = Path(path)
    if not p.exists():
        log(f"❌ Нет {path}")
        sys.exit(1)
    sj = json.loads(p.read_text(encoding="utf-8-sig"))
    urls = [s for s in sj.get("url_sources", []) if s.get("enabled", True)]
    locs = [s for s in sj.get("local_sources", []) if s.get("enabled", True)]
    return urls, locs


# ---------- Main ----------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="sources.json")
    ap.add_argument("--filters", default="filters.json")
    ap.add_argument("--out", default="docs")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--strip-trackers", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    flt = load_filters(args.filters)
    url_srcs, local_srcs = load_sources(args.sources)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_m3u = out_dir / "all_cleaned.m3u"
    rej_csv = out_dir / "rejected_clean.csv"
    stats_json = out_dir / "cleaned_stats.json"
    body_path = out_dir / "_cleaned_body.tmp"

    log(f"📥 Источников: {len(url_srcs)} URL + {len(local_srcs)} local")
    log(f"🧹 Фильтры: name={len(flt['name_exclude'])} "
        f"domain={len(flt['domain_blocklist'])} "
        f"shortener={len(flt['shortener_blocklist'])}")

    session = make_session()
    seen = set()
    per_source = []
    reasons = Counter()
    total_raw = total_kept = total_rej = 0
    header_saved: str | None = None
    directives_kept = 0

    def write_entry(fout, attrs, name, directives, url):
        nonlocal directives_kept
        attr_str = " ".join(f'{k}="{v}"' for k, v in attrs.items())
        if attr_str:
            fout.write(f"#EXTINF:-1 {attr_str},{name}\n")
        else:
            fout.write(f"#EXTINF:-1,{name}\n")
        for d in directives:
            fout.write(d + "\n")
            directives_kept += 1
        fout.write(url + "\n")

    def process(lines, src_name: str, kind: str):
        nonlocal total_raw, total_kept, total_rej
        raw = kept = rej = 0
        for attrs, name, directives, url in iter_m3u(lines):
            raw += 1
            url = url.strip().strip('"').strip("'")
            if args.strip_trackers and url:
                url = strip_tracking(url)
            reason = reject_reason(url, name, flt)
            if reason is None and url in seen:
                reason = "duplicate_url"
            if reason:
                w.writerow([src_name, name, url, reason])
                reasons[reason] += 1
                rej += 1
                continue
            seen.add(url)
            write_entry(fout, attrs, name, directives, url)
            kept += 1
        per_source.append({"name": src_name, "type": kind,
                           "raw": raw, "kept": kept, "rejected": rej})
        total_raw += raw
        total_kept += kept
        total_rej += rej
        log(f"  ✅ [{src_name}] {raw} → {kept} (отсеяно {rej})")

    with body_path.open("w", encoding="utf-8") as fout, \
         rej_csv.open("w", encoding="utf-8-sig", newline="") as fcsv:
        w = csv.writer(fcsv)
        w.writerow(["source", "name", "url", "reason"])

        # --- URL-источники ---
        for src in url_srcs:
            name, url = src.get("name", "?"), src.get("url", "")
            if not url:
                continue
            try:
                gen = stream_lines(session, url, args.timeout)
                header_line = None
                buffered = []
                for line in gen:
                    if not line.strip():
                        continue
                    if line.strip().startswith("#EXTM3U"):
                        header_line = line.strip()
                        break
                    buffered.append(line)
                    if len(buffered) > 5:
                        break
                if header_line and header_saved is None:
                    header_saved = header_line

                def chained():
                    for l in buffered:
                        yield l
                    for l in gen:
                        yield l

                process(chained(), name, "url")
            except Exception as e:
                log(f"  ❌ [{name}] {e}")
                per_source.append({"name": name, "type": "url",
                                   "raw": 0, "kept": 0, "rejected": 0,
                                   "error": str(e)})

        # --- Локальные источники ---
        for src in local_srcs:
            name, path = src.get("name", "?"), src.get("path", "")
            if not path or not Path(path).exists():
                log(f"  ⏭  [{name}] нет файла {path}")
                continue
            try:
                with open(path, "r", encoding="utf-8-sig", errors="replace") as lf:
                    header_line = None
                    for line in lf:
                        if not line.strip():
                            continue
                        if line.strip().startswith("#EXTM3U"):
                            header_line = line.strip()
                        break
                if header_line and header_saved is None:
                    header_saved = header_line
                process(local_lines(path), name, "local")
            except Exception as e:
                log(f"  ❌ [{name}] {e}")

    # Собираем итоговый файл: шапка + тело
    with out_m3u.open("w", encoding="utf-8") as fo, \
         body_path.open("r", encoding="utf-8") as fi:
        fo.write((header_saved or "#EXTM3U") + "\n")
        for line in fi:
            fo.write(line)
    body_path.unlink(missing_ok=True)

    stats = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_sec": round(time.time() - t0, 1),
        "strip_trackers": bool(args.strip_trackers),
        "header_saved": header_saved,
        "directives_kept": directives_kept,
        "sources": per_source,
        "totals": {"raw": total_raw, "kept": total_kept, "rejected": total_rej},
        "reasons": dict(reasons),
    }
    stats_json.write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    log("")
    log("📊 Причины отсева:")
    for r, n in reasons.most_common():
        log(f"   ❌ {r}: {n}")
    log("")
    log(f"📌 Сохранено #EXTVLCOPT-директив: {directives_kept}")
    log(f"📌 Шапка: {header_saved or '(нет)'}")
    log(f"✅ {total_raw} → {total_kept} (отсеяно {total_rej})")
    log(f"📄 {out_m3u}")
    log(f"⏱  {round(time.time() - t0, 1)}с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
