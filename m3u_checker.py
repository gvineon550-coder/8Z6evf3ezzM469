#!/usr/bin/env python3
"""
M3U Checker v10 — PWA, темы, CSV, скорость, split-all, дедуп по имени,
алерт источников, weekly backup.
"""
import os
import re
import sys
import csv
import time
import html
import json
import base64
import sqlite3
import logging
import argparse
import threading
import datetime
import requests
import urllib3
import concurrent.futures

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

try:
    import qrcode
    HAS_QR = True
except ImportError:
    HAS_QR = False


USER_AGENTS = [
    ('wink',     'WINK/RT_(Android_TV/11)_WinkPlayer_AppleWebKit/537.36'),
    ('vlc',      'VLC/3.0.20 LibVLC/3.0.20'),
    ('tivimate', 'TiviMate/4.7.0 (Linux;Android 11) ExoPlayerLib/2.18.1'),
    ('smarttv',  'Mozilla/5.0 (SMART-TV; Linux; Tizen 6.0) AppleWebKit/537.36'),
]
DEFAULT_UA = USER_AGENTS[0][1]
UA_LINE = f'#EXTVLCOPT:http-user-agent={DEFAULT_UA}'

HEADER_LINE = (
    '#EXTM3U url-tvg="http://epg.one/epg.xml; '
    'http://uztv.su/uploads/channelsarch/channels.xml; '
    'https://iptvx.one/EPG_NOARCH; '
    'http://gabbarit.drm-play.com/epg_lite.xml.gz; '
    'http://epg.cdntv.online/lite.xml; '
    'http://epg.it999.ru/epg.xml; '
    'http://iptv-content.rv77.pw/guide-lite.xml"'
)

IPTV_ORG_URLS = [
    'https://cdn.jsdelivr.net/gh/iptv-org/api@gh-pages/channels.json',
    'https://cdn.jsdelivr.net/gh/iptv-org/api@master/channels.json',
    'https://raw.githubusercontent.com/iptv-org/api/gh-pages/channels.json',
    'https://raw.githubusercontent.com/iptv-org/api/master/channels.json',
]
IPTV_ORG_CACHE = '_cache_sources/iptv_org_channels.json'
IPTV_ORG_TTL_DAYS = 7

HTML_PREFIXES = (
    b'<!DOCTYPE', b'<html', b'<HTML',
    b'<head', b'<HEAD', b'<?xml', b'<error',
)
BOM = b'\xef\xbb\xbf'
WHITESPACE = b' \t\r\n'
SLOW_THRESHOLD = 2.0
HISTORY_MAX = 30


log = logging.getLogger('m3u')
CFG = None
CATEGORIES = {}
FILTERS = {}
IPTV_LOGOS = {'by_id': {}, 'by_name': {}}


# ---------- ЛОГИ ----------
def setup_logging(path, quiet):
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S')
    fh = logging.FileHandler(path, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if not quiet:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.WARNING)
        ch.setFormatter(fmt)
        log.addHandler(ch)


def emit(text, pbar=None):
    if CFG.quiet:
        return
    if pbar is not None and HAS_TQDM:
        tqdm.write(text)
    else:
        print(text)


# ---------- КЭШ ----------
class UrlCache:
    def __init__(self, path, ttl):
        self.ttl = ttl
        self.lock = threading.Lock()
        self.enabled = bool(path)
        if not self.enabled:
            self.conn = None
            return
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS cache (
            url TEXT NOT NULL, kind TEXT NOT NULL,
            ok INTEGER NOT NULL, ts REAL NOT NULL,
            PRIMARY KEY (url, kind))""")
        self.conn.commit()

    def get(self, url, kind):
        if not self.enabled:
            return None
        with self.lock:
            row = self.conn.execute(
                "SELECT ok, ts FROM cache WHERE url=? AND kind=?",
                (url, kind)).fetchone()
        if not row:
            return None
        ok, ts = row
        if time.time() - ts > self.ttl:
            return None
        return bool(ok)

    def put(self, url, kind, ok):
        if not self.enabled:
            return
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO cache (url, kind, ok, ts) VALUES (?,?,?,?)",
                (url, kind, int(ok), time.time()))
            self.conn.commit()

    def close(self):
        if self.enabled and self.conn:
            with self.lock:
                self.conn.close()


def load_json(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            return json.load(f)
    except Exception as e:
        log.error("Чтение %s: %s", path, e)
        return {}


def load_sources_config(path):
    data = {'url_sources': [], 'local_sources': [], 'logo_sources': []}
    data.update(load_json(path))
    env_b64 = os.environ.get('SOURCES_JSON_B64')
    if env_b64:
        try:
            data.update(json.loads(base64.b64decode(env_b64).decode('utf-8')))
        except Exception as e:
            log.warning("SOURCES_JSON_B64: %s", e)
    return data


def normalize_name(name):
    if not name:
        return ''
    n = name.lower()
    n = re.sub(r'\b(hd|fhd|uhd|4k|8k|sd|hevc|h265|h\.265|h264|h\.264|mp4|hq|lq)\b', '', n)
    n = re.sub(r'[^a-zа-яё0-9]+', ' ', n)
    return re.sub(r'\s+', ' ', n).strip()


# ---------- IPTV-ORG ----------
def load_iptv_org_logos():
    global IPTV_LOGOS
    if CFG.no_iptv_logos:
        emit("Логотипы iptv-org: отключено")
        return
    os.makedirs(os.path.dirname(IPTV_ORG_CACHE) or '.', exist_ok=True)
    fresh = False
    if os.path.isfile(IPTV_ORG_CACHE):
        age = (time.time() - os.path.getmtime(IPTV_ORG_CACHE)) / 86400
        fresh = age < IPTV_ORG_TTL_DAYS
    if not fresh:
        got = False
        for url in IPTV_ORG_URLS:
            try:
                r = requests.get(url, timeout=30, verify=CFG.verify_ssl,
                                 headers={'User-Agent': DEFAULT_UA})
                if r.status_code != 200:
                    continue
                data = r.json()
                if not isinstance(data, list) or not data:
                    continue
                with open(IPTV_ORG_CACHE, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False)
                emit(f"  База iptv-org: {len(data)} каналов")
                got = True
                break
            except Exception as e:
                log.debug("iptv-org %s: %s", url, e)
        if not got and not os.path.isfile(IPTV_ORG_CACHE):
            emit("  База iptv-org недоступна")
            return
    try:
        with open(IPTV_ORG_CACHE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        log.warning("Кэш iptv-org: %s", e)
        return
    by_id, by_name = {}, {}
    for ch in data:
        if not isinstance(ch, dict):
            continue
        logo = ch.get('logo')
        if not logo and isinstance(ch.get('logos'), list) and ch['logos']:
            l0 = ch['logos'][0]
            logo = l0.get('url') if isinstance(l0, dict) else l0
        if not logo:
            continue
        cid = ch.get('id')
        if cid:
            by_id[cid] = logo
        for nf in ('name', 'alt_names'):
            val = ch.get(nf)
            vals = val if isinstance(val, list) else ([val] if isinstance(val, str) else [])
            for v in vals:
                if not isinstance(v, str):
                    continue
                k = normalize_name(v)
                if k and k not in by_name:
                    by_name[k] = logo
    IPTV_LOGOS['by_id'] = by_id
    IPTV_LOGOS['by_name'] = by_name
    emit(f"  Логотипов: {len(by_id)} по id, {len(by_name)} по имени")


# ---------- ИСТОЧНИКИ ----------
def download_url_sources(url_sources, cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    results = []
    for src in url_sources:
        if not src.get('enabled', True):
            continue
        name = src.get('name') or 'url'
        url = src.get('url')
        if not url:
            continue
        out = os.path.join(cache_dir, f'{name}.m3u')
        try:
            r = requests.get(url, timeout=CFG.timeout, verify=CFG.verify_ssl,
                             headers={'User-Agent': DEFAULT_UA})
            r.raise_for_status()
            with open(out, 'w', encoding='utf-8') as f:
                f.write(r.text)
            emit(f"  OK {name} ({len(r.text)} байт)")
            results.append((name, True, len(r.text)))
        except requests.RequestException as e:
            emit(f"  FAIL {name} ({e})")
            results.append((name, False, str(e)))
    return results


# ---------- ПАРСИНГ ----------
def split_extinf(line):
    in_q = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ',' and not in_q:
            return line[:i], line[i + 1:].strip()
    return line, ''


def get_name(line):
    _, n = split_extinf(line)
    return n


def get_group(line):
    m = re.search(r'group-title="([^"]*)"', line)
    return m.group(1) if m else ''


def clean_extinf(line):
    dm = re.match(r'#EXTINF:\s*(-?\d+)', line)
    dur = dm.group(1) if dm else '-1'
    tid = re.search(r'tvg-id="([^"]*)"', line)
    lg = re.search(r'tvg-logo="([^"]*)"', line)
    gr = re.search(r'group-title="([^"]*)"', line)
    nm = get_name(line)
    parts = [f'#EXTINF:{dur}']
    if tid and tid.group(1):
        parts.append(f'tvg-id="{tid.group(1)}"')
    if lg and lg.group(1):
        parts.append(f'tvg-logo="{lg.group(1)}"')
    if gr and gr.group(1):
        parts.append(f'group-title="{gr.group(1)}"')
    return ' '.join(parts) + ',' + nm


def set_group_in_extinf(extinf, new_group):
    new_group = new_group.replace('"', "'")
    if re.search(r'group-title="[^"]*"', extinf):
        return re.sub(r'group-title="[^"]*"', f'group-title="{new_group}"', extinf)
    return re.sub(r'^(#EXTINF:-?\d+)', rf'\1 group-title="{new_group}"', extinf, count=1)


def set_logo_in_extinf(extinf, logo):
    extinf = re.sub(r'\s?tvg-logo="[^"]*"', '', extinf)
    if not logo:
        return extinf
    logo = logo.replace('"', "'")
    if 'tvg-id="' in extinf:
        return re.sub(r'(tvg-id="[^"]*")',
                      lambda m: f'{m.group(1)} tvg-logo="{logo}"', extinf, count=1)
    return re.sub(r'^(#EXTINF:-?\d+)',
                  lambda m: f'{m.group(1)} tvg-logo="{logo}"', extinf, count=1)


def compile_patterns(patterns):
    out = []
    for p in patterns or []:
        try:
            out.append(re.compile(p))
        except re.error as e:
            log.warning("regex %s: %s", p, e)
    return out


def is_filtered(name, group, url):
    for p in FILTERS['_name_block']:
        if p.search(name or ''):
            return True, 'name_blocklist'
    for p in FILTERS['_name_exclude']:
        if p.search(name or ''):
            return True, 'name_exclude'
    for p in FILTERS['_group_exclude']:
        if p.search(group or ''):
            return True, 'group_exclude'
    for p in FILTERS['_url_block']:
        if p.search(url):
            return True, 'url_blocklist'
    low = url.lower().split('?')[0]
    for ext in FILTERS.get('url_extension_blocklist', []):
        if low.endswith(ext):
            return True, 'url_extension'
    for s in FILTERS.get('shortener_blocklist', []):
        if s in url:
            return True, 'shortener'
    for port in FILTERS.get('port_blocklist', []):
        if port in url:
            return True, 'port'
    for p in FILTERS['_suspicious']:
        if p.search(url):
            return True, 'suspicious'
    for d in FILTERS.get('domain_blocklist', []):
        if d in url:
            return True, 'domain'
    for p in FILTERS['_malformed']:
        if p.search(url):
            return True, 'malformed'
    if FILTERS.get('block_all_ip_urls') and re.match(r'^https?://\d+\.\d+\.\d+\.\d+', url):
        return True, 'ip_url'
    if FILTERS.get('block_ipv6_urls') and re.match(r'^https?://\[', url):
        return True, 'ipv6'
    return False, None


def categorize(extinf, url, source_name):
    current = get_group(extinf)
    name = get_name(extinf)
    for up in CATEGORIES.get('url_patterns', []):
        try:
            if re.search(up['pattern'], url) and up.get('force'):
                return up['group']
        except re.error:
            pass
    if current:
        aliases = CATEGORIES.get('group_aliases', {})
        return aliases.get(current, current)
    for up in CATEGORIES.get('url_patterns', []):
        try:
            if re.search(up['pattern'], url):
                return up['group']
        except re.error:
            pass
    if name:
        for np in CATEGORIES.get('name_patterns', []):
            try:
                if re.search(np['pattern'], name):
                    return np['group']
            except re.error:
                pass
    return CATEGORIES.get('source_defaults', {}).get(source_name, 'Разное')


def add_emoji(group):
    emoji = CATEGORIES.get('group_emoji', {})
    base = group
    for k in emoji:
        if group == k or group.endswith(' ' + k):
            base = k
            break
    return f"{emoji[base]} {base}" if base in emoji else group


def load_txt_logos(dirs):
    logos = {}
    files = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.lower().endswith('.txt') and os.path.isfile(os.path.join(d, f)):
                files.append(os.path.join(d, f))
    for p in files:
        try:
            with open(p, 'r', encoding='utf-8-sig', errors='ignore') as f:
                lines = f.readlines()
        except Exception:
            continue
        for line in lines:
            line = line.strip()
            if not line.startswith('#EXTINF'):
                continue
            lg = re.search(r'tvg-logo="([^"]*)"', line)
            if not lg or not lg.group(1):
                continue
            n = get_name(line)
            if n and n.lower() not in logos:
                logos[n.lower()] = lg.group(1)
    if files:
        emit(f"Логотипов из .txt: {len(logos)}")
    return logos


# ---------- ПРОВЕРКИ ----------
def _try_stream(url, ua):
    try:
        t0 = time.time()
        with requests.get(url, headers={'User-Agent': ua}, stream=True,
                          timeout=CFG.timeout, allow_redirects=True,
                          verify=CFG.verify_ssl) as r:
            elapsed = time.time() - t0
            if r.status_code != 200:
                return False, elapsed
            chunk = next(r.iter_content(chunk_size=512), b'')
            if not chunk:
                return False, elapsed
            h = chunk.lstrip(WHITESPACE)
            if h.startswith(BOM):
                h = h[len(BOM):].lstrip(WHITESPACE)
            if h.startswith(b'#EXTM3U'):
                return True, elapsed
            for p in HTML_PREFIXES:
                if h.startswith(p):
                    return False, elapsed
            return True, elapsed
    except requests.RequestException:
        return False, None


def _check_stream(url):
    uas = USER_AGENTS if CFG.multi_ua else USER_AGENTS[:1]
    best_elapsed = None
    for name, ua in uas:
        ok, el = _try_stream(url, ua)
        if ok:
            return True, name, el
        if el is not None and (best_elapsed is None or el < best_elapsed):
            best_elapsed = el
    return False, None, best_elapsed


def check_stream(url, cache):
    c = cache.get(url, 'stream')
    if c is not None:
        return c, 'cached', None
    ok, n, el = _check_stream(url)
    cache.put(url, 'stream', ok)
    return ok, (n or DEFAULT_UA), el


def _check_logo(url):
    try:
        r = requests.head(url, headers={'User-Agent': DEFAULT_UA},
                          timeout=CFG.logo_timeout, allow_redirects=True,
                          verify=CFG.verify_ssl)
        if r.status_code in (400, 403, 405):
            with requests.get(url, headers={'User-Agent': DEFAULT_UA}, stream=True,
                              timeout=CFG.logo_timeout, allow_redirects=True,
                              verify=CFG.verify_ssl) as g:
                return g.status_code == 200
        return r.status_code == 200
    except requests.RequestException:
        return False


def check_logo(url, cache):
    c = cache.get(url, 'logo')
    if c is not None:
        return c
    ok = _check_logo(url)
    cache.put(url, 'logo', ok)
    return ok


def resolve_logo(extinf, url, txt_logos, cache):
    m = re.search(r'tvg-logo="([^"]*)"', extinf)
    if m and m.group(1) and check_logo(m.group(1), cache):
        return m.group(1)
    tid = re.search(r'tvg-id="([^"]*)"', extinf)
    if tid and tid.group(1):
        l = IPTV_LOGOS['by_id'].get(tid.group(1))
        if l and check_logo(l, cache):
            return l
    name = get_name(extinf)
    if name:
        l = IPTV_LOGOS['by_name'].get(normalize_name(name))
        if l and check_logo(l, cache):
            return l
        l = txt_logos.get(name.lower())
        if l and check_logo(l, cache):
            return l
    return None


def process_channel(index, extinf, url, txt_logos, cache, source_name):
    name = get_name(extinf)
    group = get_group(extinf)
    filtered, reason = is_filtered(name, group, url)
    if filtered:
        return index, 'filtered', None, None, reason, None, None, None
    ok, ua, elapsed = check_stream(url, cache)
    if not ok:
        return index, 'dead', None, None, None, None, None, None
    extinf = clean_extinf(extinf)
    extinf = set_logo_in_extinf(extinf, resolve_logo(extinf, url, txt_logos, cache))
    new_group = categorize(extinf, url, source_name)
    extinf = set_group_in_extinf(extinf, add_emoji(new_group))
    return index, 'ok', extinf, url, None, ua, new_group, elapsed


def parse_playlist(filename):
    try:
        with open(filename, 'r', encoding='utf-8-sig', errors='ignore') as f:
            lines = f.readlines()
    except Exception as e:
        log.error("Чтение %s: %s", filename, e)
        return None
    if not lines or not lines[0].lstrip().startswith("#EXTM3U"):
        return None
    channels, cur = [], None
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            cur = line
        elif not line.startswith("#") and cur:
            channels.append((cur, line))
            cur = None
    return channels


def process_playlist(filename, label, txt_logos, cache, check=True):
    emit(f"\n>>> [{label}] {filename} {'(без проверки)' if not check else ''}")
    channels = parse_playlist(filename)
    if channels is None:
        return None
    if not channels:
        return {'label': label, 'channels': [], 'total': 0, 'ok': 0,
                'filtered': 0, 'groups': {}, 'ua_stats': {}, 'slow': [],
                'fastest': None, 'avg_elapsed': None}

    emit(f"    Каналов: {len(channels)}")

    if not check:
        out, elapsed_list = [], []
        for i, (extinf, url) in enumerate(channels):
            name = get_name(extinf)
            group = get_group(extinf)
            filtered, _ = is_filtered(name, group, url)
            if filtered:
                continue
            extinf = clean_extinf(extinf)
            extinf = set_logo_in_extinf(extinf, resolve_logo(extinf, url, txt_logos, cache))
            extinf = set_group_in_extinf(extinf, add_emoji(categorize(extinf, url, label)))
            out.append((i, extinf, url))
        ordered = group_channels(out)
        gs = {}
        for _, e, _ in ordered:
            g = get_group(e) or '(без группы)'
            gs[g] = gs.get(g, 0) + 1
        return {'label': label, 'channels': [(e, u) for _, e, u in ordered],
                'total': len(channels), 'ok': len(ordered),
                'filtered': len(channels) - len(ordered),
                'groups': gs, 'ua_stats': {'skipped': len(ordered)},
                'slow': [], 'fastest': None, 'avg_elapsed': None}

    pbar = None
    if HAS_TQDM and not CFG.quiet and not CFG.no_progress:
        pbar = tqdm(total=len(channels), desc=label, unit='ch', ncols=90, leave=True)

    valid, ok_cnt, err_cnt, filt_cnt = [], 0, 0, 0
    ua_stats, filt_by_reason = {}, {}
    slow_list, elapsed_list = [], []

    with concurrent.futures.ThreadPoolExecutor(max_workers=CFG.workers) as ex:
        futs = [ex.submit(process_channel, i, e, u, txt_logos, cache, label)
                for i, (e, u) in enumerate(channels)]
        for fu in concurrent.futures.as_completed(futs):
            try:
                idx, status, extinf, url, msg, ua, grp, el = fu.result()
            except Exception as e:
                err_cnt += 1
                log.exception("Ошибка: %s", e)
                if pbar:
                    pbar.update(1)
                continue
            if status == 'ok':
                valid.append((idx, extinf, url))
                ok_cnt += 1
                ua_stats[ua] = ua_stats.get(ua, 0) + 1
                if el is not None:
                    elapsed_list.append(el)
                    if el >= SLOW_THRESHOLD:
                        slow_list.append((get_name(extinf), url, round(el, 2)))
            elif status == 'filtered':
                filt_cnt += 1
                filt_by_reason[msg] = filt_by_reason.get(msg, 0) + 1
            if pbar:
                pbar.update(1)
                pbar.set_postfix(ok=ok_cnt, filt=filt_cnt, err=err_cnt)
    if pbar:
        pbar.close()

    valid.sort(key=lambda x: x[0])
    ordered = group_channels(valid)
    gs = {}
    for _, e, _ in ordered:
        g = get_group(e) or '(без группы)'
        gs[g] = gs.get(g, 0) + 1

    avg_el = round(sum(elapsed_list) / len(elapsed_list), 2) if elapsed_list else None
    fastest = round(min(elapsed_list), 2) if elapsed_list else None
    slow_list.sort(key=lambda x: -x[2])

    emit(f"    Рабочих: {ok_cnt}, фильтр: {filt_cnt}, ошибок: {err_cnt}")
    return {'label': label, 'channels': [(e, u) for _, e, u in ordered],
            'total': len(channels), 'ok': ok_cnt, 'filtered': filt_cnt,
            'groups': gs, 'ua_stats': ua_stats, 'filter_reasons': filt_by_reason,
            'slow': slow_list, 'fastest': fastest, 'avg_elapsed': avg_el}


def dedup_by_name_fn(channels):
    """Убирает дубли по нормализованному имени, оставляет первый."""
    seen = set()
    out = []
    for extinf, url in channels:
        key = normalize_name(get_name(extinf))
        if not key:
            out.append((extinf, url))
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append((extinf, url))
    return out


def group_channels(channels):
    priority = CATEGORIES.get('priority_groups', [])
    prio_map = {p: i for i, p in enumerate(priority)}
    sort_in = CATEGORIES.get('sort_channels_in_group', True)
    grouped = {}
    for idx, extinf, url in channels:
        g = get_group(extinf) or '(без группы)'
        grouped.setdefault(g, []).append((idx, extinf, url))

    def gsort(g):
        return (prio_map.get(g, 9999), g.lower())

    ordered = []
    for g in sorted(grouped.keys(), key=gsort):
        items = grouped[g]
        if sort_in:
            items = sorted(items, key=lambda x: get_name(x[1]).lower())
        ordered.extend(items)
    return ordered


def write_playlist(path, channels):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(HEADER_LINE + "\n")
        for extinf, url in channels:
            f.write(extinf + "\n")
            f.write(UA_LINE + "\n")
            f.write(url + "\n")


def slugify_group(g):
    """Превращает группу в безопасное имя файла."""
    s = re.sub(r'^\S+\s+', '', g)
    s = s.lower()
    translit = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
    }
    out = []
    for ch in s:
        if ch in translit:
            out.append(translit[ch])
        elif ch.isalnum():
            out.append(ch)
        else:
            out.append('_')
    return re.sub(r'_+', '_', ''.join(out)).strip('_')


def write_splits(channels, docs_dir, split_all=False):
    """Создаёт отдельные плейлисты. Если split_all — для каждой группы."""
    created = []
    splits = dict(CATEGORIES.get('split_playlists', {}))

    if split_all:
        all_groups = set()
        for extinf, _ in channels:
            g = get_group(extinf)
            if g and g != '(без группы)':
                all_groups.add(g)
        for g in all_groups:
            slug = slugify_group(g)
            if not slug:
                continue
            fname = f"group_{slug}.m3u8"
            if fname not in splits:
                splits[fname] = [g]

    for fname, groups in splits.items():
        wanted = set(groups)
        sel = []
        for extinf, url in channels:
            g = get_group(extinf)
            base = re.sub(r'^\S+\s+', '', g) if g else ''
            if g in wanted or base in wanted:
                sel.append((extinf, url))
        if sel:
            write_playlist(os.path.join(docs_dir, fname), sel)
            created.append((fname, len(sel)))
    return created


def write_csv(channels, path):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['name', 'group', 'logo', 'url'])
        for extinf, url in channels:
            name = get_name(extinf)
            group = get_group(extinf)
            m = re.search(r'tvg-logo="([^"]*)"', extinf)
            logo = m.group(1) if m else ''
            w.writerow([name, group, logo, url])


# ---------- ИСТОРИЯ ----------
def load_history(path):
    if not os.path.isfile(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            d = json.load(f)
            return d[-HISTORY_MAX:] if isinstance(d, list) else []
    except Exception:
        return []


def save_history(path, history):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(history[-HISTORY_MAX:], f, ensure_ascii=False, indent=2)


def render_history_svg(history):
    if not history:
        return '<div class="section"><h2>История</h2><p style="color:var(--muted)">Данных пока нет</p></div>'
    W, H, P = 900, 200, 30
    ok_vals = [h.get('ok', 0) for h in history]
    mx = max(ok_vals) if ok_vals else 1
    mx = max(mx, 1)
    n = len(history)
    step = (W - 2 * P) / max(n - 1, 1)
    pts = []
    for i, v in enumerate(ok_vals):
        x = P + i * step
        y = H - P - (v / mx) * (H - 2 * P)
        pts.append((x, y))
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"M{P},{H-P} L" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + f" L{W-P},{H-P} Z"
    labels = ""
    for i in range(0, n, max(1, n // 6)):
        x = P + i * step
        d = history[i].get('date', '')[:10]
        labels += f'<text x="{x:.0f}" y="{H-8}" fill="var(--muted)" font-size="10" text-anchor="middle">{d}</text>'
    grid = ""
    for i in range(5):
        y = P + i * (H - 2 * P) / 4
        val = int(mx * (1 - i / 4))
        grid += f'<line x1="{P}" y1="{y:.0f}" x2="{W-P}" y2="{y:.0f}" stroke="var(--border)" stroke-width="1"/>'
        grid += f'<text x="4" y="{y+3:.0f}" fill="var(--muted)" font-size="10">{val}</text>'
    return f'''<div class="section"><h2>История (последние {n})</h2>
<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto">
{grid}
<path d="{area}" fill="#4ade80" fill-opacity="0.15"/>
<polyline points="{poly}" fill="none" stroke="#4ade80" stroke-width="2"/>
{labels}
</svg></div>'''


# ---------- CSS/HTML ОБЩЕЕ ----------
COMMON_CSS = """
*{box-sizing:border-box}
:root{
  --bg:#0f1115; --panel:#181b20; --border:#23272e;
  --text:#e6e6e6; --muted:#8a8f98; --accent:#60a5fa;
}
[data-theme="light"]{
  --bg:#f6f7f9; --panel:#ffffff; --border:#e4e6ea;
  --text:#1a1d21; --muted:#6b7280; --accent:#2563eb;
}
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);
color:var(--text);margin:0;padding:24px;max-width:1100px;margin:0 auto;
transition:background .2s,color .2s}
h1{margin:0 0 4px;font-size:22px} h2{font-size:15px;margin:0 0 12px}
.sub{color:var(--muted);font-size:13px;margin-bottom:24px}
a{color:var(--accent)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
gap:12px;margin-bottom:24px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px}
.card .k{font-size:11px;color:var(--muted);text-transform:uppercase}
.card .v{font-size:22px;font-weight:600;margin-top:6px}
.card .v.good{color:#4ade80} .card .v.bad{color:#f87171}
.card .v.warn{color:#facc15}
.section{background:var(--panel);border:1px solid var(--border);border-radius:10px;
padding:16px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border)}
th{color:var(--muted);font-weight:500;font-size:11px;text-transform:uppercase}
.tag{display:inline-block;padding:2px 8px;border-radius:6px;background:var(--border);
font-size:12px;color:var(--muted);margin:2px}
.back{display:inline-block;margin-bottom:16px;color:var(--accent);text-decoration:none}
.box{background:var(--panel);border:1px solid var(--border);border-radius:10px;
padding:18px;margin-bottom:12px}
code{background:var(--bg);padding:3px 8px;border-radius:6px;font-size:13px;
word-break:break-all;color:#facc15}
.theme-btn{position:fixed;top:16px;right:16px;background:var(--panel);
border:1px solid var(--border);color:var(--text);width:40px;height:40px;
border-radius:50%;cursor:pointer;font-size:18px;z-index:100;line-height:1}
.theme-btn:hover{background:var(--border)}
"""

THEME_JS = """
<script>
(function(){
  const saved = localStorage.getItem('theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  document.addEventListener('DOMContentLoaded', function(){
    const btn = document.querySelector('.theme-btn');
    if(!btn) return;
    btn.textContent = saved === 'dark' ? '☀' : '☾';
    btn.addEventListener('click', function(){
      const cur = document.documentElement.getAttribute('data-theme');
      const nxt = cur === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', nxt);
      localStorage.setItem('theme', nxt);
      btn.textContent = nxt === 'dark' ? '☀' : '☾';
    });
  });
})();
</script>
"""

THEME_BTN = '<button class="theme-btn" aria-label="Theme">☀</button>'


# ---------- HTML ----------
REPORT_T = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>M3U Check v10 - {date}</title>
<style>{common_css}</style>
</head><body>
{theme_btn}
<a class="back" href="index.html">← на главную</a>
<h1>M3U Check v10</h1>
<div class="sub">{date} · источников: {n_playlists}</div>
<div class="cards">
<div class="card"><div class="k">Проверено</div><div class="v">{total}</div></div>
<div class="card"><div class="k">Рабочих</div><div class="v good">{ok}</div></div>
<div class="card"><div class="k">Отфильтровано</div><div class="v warn">{filtered}</div></div>
<div class="card"><div class="k">Мёртвых</div><div class="v bad">{dead}</div></div>
<div class="card"><div class="k">В merged</div><div class="v">{merged}</div></div>
<div class="card"><div class="k">Время</div><div class="v">{duration}</div></div>
</div>
{history_block}
{ua_block}
{slow_block}
<div class="section"><h2>По источникам</h2>
<table><thead><tr><th>Источник</th><th>Всего</th><th>Рабочих</th><th>Фильтр</th><th>%</th><th>Ср. отклик</th></tr></thead>
<tbody>{playlists_rows}</tbody></table></div>
<div class="section"><h2>Фильтры</h2>
<table><thead><tr><th>Причина</th><th>Каналов</th></tr></thead>
<tbody>{filter_rows}</tbody></table></div>
<div class="section"><h2>Группы (итог)</h2>
<table><thead><tr><th>Группа</th><th>Каналов</th></tr></thead>
<tbody>{groups_rows}</tbody></table></div>
{theme_js}
</body></html>
"""

INDEX_T = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>IPTV</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="manifest" href="manifest.json">
<link rel="icon" type="image/svg+xml" href="icon.svg">
<meta name="theme-color" content="#0f1115">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="IPTV">
<link rel="apple-touch-icon" href="icon.svg">
<style>{common_css}
.qr-wrap{{display:flex;gap:20px;align-items:center;flex-wrap:wrap}}
.qr-wrap img{{background:#fff;padding:10px;border-radius:10px;width:200px;height:200px}}
.qr-info{{flex:1;min-width:200px}}
</style></head><body>
{theme_btn}
<h1>IPTV - авто-обновляемый плейлист</h1>
<div class="sub">Обновлено: {date}</div>

<h2>Ссылка для плеера (M3U)</h2>
<div class="box qr-wrap">
{qr_block}
<div class="qr-info">
<code>{playlist_url}</code>
<div class="sub" style="margin-top:8px">Скопируй в TiviMate / VLC / OTT Navigator</div>
</div>
</div>

<h2>Разделы</h2>
<div class="box"><a href="report.html">📊 Отчёт проверки</a></div>
<div class="box"><a href="channels.html">🔍 Поиск по каналам</a></div>
<div class="box"><a href="channels.csv">📥 Скачать CSV (все каналы)</a></div>

<h2>Статистика</h2>
<div class="box">
Каналов в merged: <b>{merged_count}</b><br>
Рабочих: <b>{ok}</b> из <b>{total}</b>
</div>
{splits_block}
{theme_js}
</body></html>
"""

CHANNELS_T = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Каналы</title>
<link rel="manifest" href="manifest.json">
<link rel="icon" type="image/svg+xml" href="icon.svg">
<style>{common_css}
input,select{{background:var(--panel);border:1px solid var(--border);color:var(--text);
padding:10px 12px;border-radius:8px;font-size:14px;width:100%;
margin-bottom:10px;font-family:inherit}}
.controls{{display:grid;grid-template-columns:1fr 220px;gap:10px;margin-bottom:16px}}
@media(max-width:600px){{.controls{{grid-template-columns:1fr}}}}
th{{position:sticky;top:0;background:var(--bg)}}
tr:hover{{background:var(--panel)}}
.logo{{width:32px;height:32px;object-fit:contain;vertical-align:middle;
background:var(--border);border-radius:4px;padding:2px}}
.name{{font-weight:500}}
.group{{color:var(--muted);font-size:12px}}
.copy{{background:var(--border);border:none;color:var(--muted);padding:4px 8px;
border-radius:4px;font-size:11px;cursor:pointer}}
.copy:hover{{background:var(--accent);color:#fff}}
.stats{{color:var(--muted);font-size:13px;margin-bottom:12px}}
</style></head><body>
{theme_btn}
<a href="index.html">← на главную</a>
<h1>Каналы</h1>
<div class="sub">Всего: <span id="cnt">{total}</span> · показано: <span id="shown">{total}</span></div>
<div class="controls">
<input id="q" type="search" placeholder="Поиск по названию...">
<select id="g"><option value="">Все группы</option>{group_options}</select>
</div>
<table>
<thead><tr><th></th><th>Название</th><th>Группа</th><th></th></tr></thead>
<tbody id="tb"></tbody>
</table>
<script>
const CH = {channels_json};
const tb = document.getElementById('tb');
const q = document.getElementById('q');
const g = document.getElementById('g');
const shown = document.getElementById('shown');
function esc(s) {{ return (s||'').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])); }}
function render() {{
  const term = q.value.trim().toLowerCase();
  const grp = g.value;
  let out = [];
  for (const c of CH) {{
    if (grp && c.group !== grp) continue;
    if (term && !c.name.toLowerCase().includes(term)) continue;
    out.push(c);
  }}
  if (out.length > 500) out = out.slice(0, 500);
  const html = out.map(c => {{
    const logo = c.logo ? `<img class="logo" src="${{esc(c.logo)}}" loading="lazy" onerror="this.style.display='none'">` : '';
    return `<tr><td>${{logo}}</td>
      <td class="name">${{esc(c.name)}}</td>
      <td class="group">${{esc(c.group)}}</td>
      <td><button class="copy" data-u="${{esc(c.url)}}">URL</button></td></tr>`;
  }}).join('');
  tb.innerHTML = html || '<tr><td colspan="4" style="text-align:center;color:var(--muted);padding:24px">Ничего не найдено</td></tr>';
  shown.textContent = out.length;
  tb.querySelectorAll('.copy').forEach(b => b.addEventListener('click', e => {{
    navigator.clipboard.writeText(b.dataset.u).then(() => {{
      const t = b.textContent; b.textContent = '✓'; setTimeout(() => b.textContent = t, 1000);
    }});
  }}));
}}
q.addEventListener('input', render);
g.addEventListener('change', render);
render();
</script>
{theme_js}
</body></html>
"""

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
<rect width="192" height="192" rx="40" fill="#0f1115"/>
<rect x="24" y="48" width="144" height="96" rx="12" fill="none" stroke="#4ade80" stroke-width="6"/>
<path d="M80 80 L80 112 L110 96 Z" fill="#4ade80"/>
<circle cx="96" cy="160" r="4" fill="#8a8f98"/>
</svg>"""

MANIFEST_T = """{{
  "name": "IPTV Auto",
  "short_name": "IPTV",
  "start_url": "index.html",
  "display": "standalone",
  "background_color": "#0f1115",
  "theme_color": "#0f1115",
  "icons": [
    {{"src": "icon.svg", "sizes": "192x192", "type": "image/svg+xml", "purpose": "any"}},
    {{"src": "icon.svg", "sizes": "512x512", "type": "image/svg+xml", "purpose": "any"}}
  ]
}}
"""


def render_report(path, stats, merged, total, ok, filt, dead, dur, ua, history):
    rows_pl = []
    for st in stats:
        pct = (st['ok'] / st['total'] * 100) if st['total'] else 0
        avg = st.get('avg_elapsed')
        avg_s = f"{avg}s" if avg is not None else "—"
        rows_pl.append(
            f"<tr><td>{html.escape(st['label'])}</td><td>{st['total']}</td>"
            f"<td>{st['ok']}</td><td>{st.get('filtered', 0)}</td>"
            f"<td>{pct:.1f}%</td><td>{avg_s}</td></tr>"
        )
    all_f = {}
    for st in stats:
        for r, c in st.get('filter_reasons', {}).items():
            all_f[r] = all_f.get(r, 0) + c
    f_rows = "\n".join(f"<tr><td>{html.escape(r)}</td><td>{c}</td></tr>"
                       for r, c in sorted(all_f.items(), key=lambda x: -x[1])) \
             or "<tr><td colspan='2'>нет</td></tr>"
    all_g = {}
    for st in stats:
        for g, c in st['groups'].items():
            all_g[g] = all_g.get(g, 0) + c
    g_rows = "\n".join(f"<tr><td>{html.escape(g)}</td><td>{c}</td></tr>"
                       for g, c in sorted(all_g.items(), key=lambda x: -x[1])) \
             or "<tr><td colspan='2'>нет</td></tr>"
    ua_block = ''
    if ua:
        tags = " ".join(f"<span class='tag'>{html.escape(k)}: {v}</span>"
                        for k, v in sorted(ua.items(), key=lambda x: -x[1]))
        ua_block = f"<div class='section'><h2>По User-Agent</h2><div>{tags}</div></div>"

    all_slow = []
    for st in stats:
        for nm, u, el in st.get('slow', [])[:10]:
            all_slow.append((nm, u, el))
    all_slow.sort(key=lambda x: -x[2])
    all_slow = all_slow[:20]
    slow_block = ''
    if all_slow:
        rows = "\n".join(
            f"<tr><td>{html.escape(nm)}</td><td>{el}s</td></tr>"
            for nm, u, el in all_slow)
        slow_block = (
            f"<div class='section'><h2>Медленные каналы (>{SLOW_THRESHOLD}s)</h2>"
            f"<table><thead><tr><th>Канал</th><th>Отклик</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>")

    hist_block = render_history_svg(history)
    d = int(dur)
    out = REPORT_T.format(
        common_css=COMMON_CSS, theme_btn=THEME_BTN, theme_js=THEME_JS,
        date=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        n_playlists=len(stats), total=total, ok=ok, filtered=filt, dead=dead,
        merged=merged, duration=f"{d // 60}м {d % 60}с",
        history_block=hist_block, ua_block=ua_block, slow_block=slow_block,
        playlists_rows="\n".join(rows_pl), filter_rows=f_rows, groups_rows=g_rows)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(out)


def render_index(path, url, merged, ok, total, splits, qr_path):
    split_html = ''
    if splits:
        items = "\n".join(
            f"<div class='box'><a href='{fname}'>{fname}</a> — {cnt} каналов</div>"
            for fname, cnt in splits)
        split_html = f"<h2>Отдельные плейлисты</h2>{items}"
    if os.path.isfile(qr_path):
        qr_block = f'<img src="{os.path.basename(qr_path)}" alt="QR">'
    else:
        qr_block = ''
    out = INDEX_T.format(
        common_css=COMMON_CSS, theme_btn=THEME_BTN, theme_js=THEME_JS,
        date=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        playlist_url=url, merged_count=merged, ok=ok, total=total,
        qr_block=qr_block, splits_block=split_html)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(out)


def render_channels(path, channels):
    groups = {}
    ch_list = []
    for extinf, url in channels:
        name = get_name(extinf)
        group = get_group(extinf)
        m = re.search(r'tvg-logo="([^"]*)"', extinf)
        logo = m.group(1) if m else ''
        groups[group] = groups.get(group, 0) + 1
        ch_list.append({'name': name, 'group': group, 'logo': logo, 'url': url})
    group_opts = "\n".join(
        f'<option value="{html.escape(g)}">{html.escape(g)} ({c})</option>'
        for g, c in sorted(groups.items()))
    out = CHANNELS_T.format(
        common_css=COMMON_CSS, theme_btn=THEME_BTN, theme_js=THEME_JS,
        total=len(ch_list), group_options=group_opts,
        channels_json=json.dumps(ch_list, ensure_ascii=False))
    with open(path, 'w', encoding='utf-8') as f:
        f.write(out)


def write_pwa_assets(docs_dir):
    with open(os.path.join(docs_dir, 'icon.svg'), 'w', encoding='utf-8') as f:
        f.write(ICON_SVG)
    with open(os.path.join(docs_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
        f.write(MANIFEST_T)


def generate_qr(url, path):
    if not HAS_QR:
        return False
    try:
        img = qrcode.make(url)
        img.save(path)
        return True
    except Exception as e:
        log.warning("QR: %s", e)
        return False


# ---------- TELEGRAM ----------
def tg_send(token, chat, text):
    if not token or not chat:
        return False
    try:
        r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                          data={'chat_id': chat, 'text': text, 'parse_mode': 'HTML',
                                'disable_web_page_preview': 'true'}, timeout=15)
        return r.status_code == 200
    except requests.RequestException:
        return False


def tg_file(token, chat, path, caption=''):
    if not token or not chat or not os.path.isfile(path):
        return False
    try:
        with open(path, 'rb') as f:
            r = requests.post(f'https://api.telegram.org/bot{token}/sendDocument',
                              data={'chat_id': chat, 'caption': caption[:1000]},
                              files={'document': (os.path.basename(path), f,
                                                  'application/octet-stream')},
                              timeout=60)
        return r.status_code == 200
    except requests.RequestException:
        return False


def tg_report(stats, total, ok, filt, merged, dur, index_url,
              source_results, is_weekly):
    lines = ["<b>M3U Check v10</b>"]
    if is_weekly:
        lines.append("🗓 <i>Еженедельный отчёт</i>")
    lines.extend([
        f"Проверено: <b>{total}</b>",
        f"Рабочих: <b>{ok}</b>",
        f"Отфильтровано: <b>{filt}</b>",
        f"В merged: <b>{merged}</b>",
        f"Время: {int(dur)}с",
        "",
    ])
    for st in stats:
        pct = (st['ok'] / st['total'] * 100) if st['total'] else 0
        lines.append(f"• {html.escape(st['label'])}: {st['ok']}/{st['total']} ({pct:.0f}%)")

    bad_sources = [(n, err) for n, ok_, err in source_results if not ok_]
    if bad_sources:
        lines.append("")
        lines.append("<b>⚠️ Проблемные источники:</b>")
        for n, err in bad_sources:
            lines.append(f"• {html.escape(n)}: {html.escape(str(err))[:80]}")

    if index_url:
        lines.append("")
        lines.append(f'<a href="{index_url}">Открыть страницу</a>')
    tg_send(CFG.tg_token, CFG.tg_chat, "\n".join(lines))


# ---------- MAIN ----------
def parse_args():
    p = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('--sources', default='sources.json')
    p.add_argument('--categories', default='categories.json')
    p.add_argument('--filters', default='filters.json')
    p.add_argument('-o', '--output', default='checked_playlists')
    p.add_argument('--docs-dir', default='docs')
    p.add_argument('-m', '--merged', dest='merged_name', default='all_checked.m3u8')
    p.add_argument('--no-merge', action='store_true')
    p.add_argument('--no-dedup', action='store_true')
    p.add_argument('-w', '--workers', type=int, default=15)
    p.add_argument('--timeout', type=float, default=5.0)
    p.add_argument('--logo-timeout', type=float, default=3.0)
    p.add_argument('--no-ssl-verify', action='store_true')
    p.add_argument('--multi-ua', action='store_true')
    p.add_argument('--no-iptv-logos', action='store_true')
    p.add_argument('--split-all', action='store_true',
                   help='Отдельный плейлист для каждой группы')
    p.add_argument('--weekly-backup', action='store_true',
                   help='Отправить файлы в Telegram (для воскресного запуска)')
    p.add_argument('--cache', default='m3u_cache.sqlite')
    p.add_argument('--cache-ttl', type=int, default=3600)
    p.add_argument('--log', default='m3u_checker.log')
    p.add_argument('--no-progress', action='store_true')
    p.add_argument('-q', '--quiet', action='store_true')
    p.add_argument('--pages-url', default=os.environ.get('PAGES_URL', ''))
    p.add_argument('--tg-token', default=os.environ.get('TG_BOT_TOKEN', ''))
    p.add_argument('--tg-chat', default=os.environ.get('TG_CHAT_ID', ''))
    p.add_argument('--tg-send-merged', action='store_true')
    return p.parse_args()


def main():
    global CFG, CATEGORIES, FILTERS
    CFG = parse_args()
    CFG.verify_ssl = not CFG.no_ssl_verify
    if CFG.no_ssl_verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    setup_logging(CFG.log, CFG.quiet)

    CATEGORIES = load_json(CFG.categories)
    FILTERS = load_json(CFG.filters)
    FILTERS['_name_block'] = compile_patterns(FILTERS.get('name_blocklist', []))
    FILTERS['_name_exclude'] = compile_patterns(FILTERS.get('name_exclude', []))
    FILTERS['_group_exclude'] = compile_patterns(FILTERS.get('group_exclude', []))
    FILTERS['_url_block'] = compile_patterns(FILTERS.get('url_blocklist', []))
    FILTERS['_suspicious'] = compile_patterns(FILTERS.get('suspicious_patterns', []))
    FILTERS['_malformed'] = compile_patterns(FILTERS.get('malformed_patterns', []))

    emit("Загружаю базу логотипов iptv-org...")
    load_iptv_org_logos()

    cfg = load_sources_config(CFG.sources)
    url_sources = cfg.get('url_sources', [])
    local_sources = cfg.get('local_sources', [])

    cache_dir = '_cache_sources'
    emit(f"\nСкачиваю {len(url_sources)} URL-источников...")
    source_results = download_url_sources(url_sources, cache_dir)

    all_sources = []
    for src in url_sources:
        if not src.get('enabled', True):
            continue
        n = src.get('name') or 'url'
        p = os.path.join(cache_dir, f'{n}.m3u')
        if os.path.isfile(p):
            all_sources.append((p, n, src.get('check', True)))
    for src in local_sources:
        if not src.get('enabled', True):
            continue
        p = src.get('path')
        if p and os.path.isfile(p):
            all_sources.append((p, src.get('name') or 'local', src.get('check', True)))

    if not all_sources:
        emit("Нет источников.")
        return 1

    txt_logos = load_txt_logos(['local', '.'])
    cache = UrlCache(CFG.cache, CFG.cache_ttl)
    os.makedirs(CFG.output, exist_ok=True)
    os.makedirs(CFG.docs_dir, exist_ok=True)

    started = time.time()
    stats_list, merged_all, seen, pl = [], [], set(), 0

    try:
        for path, label, check in all_sources:
            st = process_playlist(path, label, txt_logos, cache, check=check)
            if st is None:
                continue
            safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', label)
            write_playlist(os.path.join(CFG.output, f"{safe}.m3u8"), st['channels'])
            for e, u in st['channels']:
                if not CFG.no_dedup and u in seen:
                    continue
                seen.add(u)
                merged_all.append((pl, e, u))
            stats_list.append(st)
            pl += 1

        # Дедуп по имени
        merged_pairs = [(e, u) for _, e, u in merged_all]
        if FILTERS.get('dedup_by_name'):
            before = len(merged_pairs)
            merged_pairs = dedup_by_name_fn(merged_pairs)
            emit(f"Дедуп по имени: {before} -> {len(merged_pairs)}")

        ordered_merged = [(e, u) for _, e, u in group_channels(
            [(i, e, u) for i, (e, u) in enumerate(merged_pairs)])]

        merged_path = None
        if not CFG.no_merge and ordered_merged:
            merged_path = os.path.join(CFG.docs_dir, CFG.merged_name)
            write_playlist(merged_path, ordered_merged)
            write_playlist(os.path.join(CFG.output, CFG.merged_name), ordered_merged)

        splits = write_splits(ordered_merged, CFG.docs_dir, split_all=CFG.split_all)
        for fname, cnt in splits:
            emit(f"    split: {fname} ({cnt})")

        total = sum(s['total'] for s in stats_list)
        ok = sum(s['ok'] for s in stats_list)
        filt = sum(s.get('filtered', 0) for s in stats_list)
        dead = total - ok - filt

        ua_totals = {}
        for s in stats_list:
            for k, c in s['ua_stats'].items():
                if k == 'skipped':
                    continue
                ua_totals[k] = ua_totals.get(k, 0) + c

        # История
        history_path = os.path.join(CFG.docs_dir, 'history.json')
        history = load_history(history_path)
        history.append({
            'date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
            'total': total, 'ok': ok, 'filtered': filt,
            'dead': dead, 'merged': len(ordered_merged),
        })
        save_history(history_path, history)

        # CSV
        write_csv(ordered_merged, os.path.join(CFG.docs_dir, 'channels.csv'))

        # HTML
        render_report(os.path.join(CFG.docs_dir, 'report.html'),
                      stats_list, len(ordered_merged), total, ok, filt, dead,
                      time.time() - started, ua_totals, history)

        pages = CFG.pages_url.rstrip('/')
        purl = f'{pages}/{CFG.merged_name}' if pages else CFG.merged_name
        iurl = f'{pages}/index.html' if pages else ''
        qr_path = os.path.join(CFG.docs_dir, 'qr.png')
        if pages and HAS_QR:
            generate_qr(purl, qr_path)
        render_index(os.path.join(CFG.docs_dir, 'index.html'),
                     purl, len(ordered_merged), ok, total, splits, qr_path)
        render_channels(os.path.join(CFG.docs_dir, 'channels.html'), ordered_merged)
        write_pwa_assets(CFG.docs_dir)

        emit(f"\nГотово за {int(time.time() - started)}с. "
             f"OK={ok}, фильтр={filt}, merged={len(ordered_merged)}")

        # Telegram
        if CFG.tg_token and CFG.tg_chat:
            tg_report(stats_list, total, ok, filt, len(ordered_merged),
                      time.time() - started, iurl, source_results,
                      is_weekly=CFG.weekly_backup)
            if CFG.tg_send_merged and merged_path:
                tg_file(CFG.tg_token, CFG.tg_chat, merged_path,
                        caption="Merged плейлист")

            if CFG.weekly_backup:
                for fn in ('report.html', 'channels.csv', 'channels.html'):
                    fp = os.path.join(CFG.docs_dir, fn)
                    if os.path.isfile(fp):
                        tg_file(CFG.tg_token, CFG.tg_chat, fp,
                                caption=f"Weekly backup: {fn}")

    finally:
        cache.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
