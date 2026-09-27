#!/usr/bin/env python3
"""
M3U Checker v6.2 — проверка IPTV-плейлистов.
URL-источники + локальные файлы + Telegram + GitHub Pages.
"""
import os
import re
import sys
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

HTML_PREFIXES = (
    b'<!DOCTYPE', b'<html', b'<HTML',
    b'<head', b'<HEAD', b'<?xml', b'<error',
)
BOM = b'\xef\xbb\xbf'
WHITESPACE = b' \t\r\n'

log = logging.getLogger('m3u')
CFG = None


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


class UrlCache:
    def __init__(self, path, ttl):
        self.ttl = ttl
        self.lock = threading.Lock()
        self.enabled = bool(path)
        if not self.enabled:
            self.conn = None
            return
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                url  TEXT NOT NULL,
                kind TEXT NOT NULL,
                ok   INTEGER NOT NULL,
                ts   REAL NOT NULL,
                PRIMARY KEY (url, kind)
            )
        """)
        self.conn.commit()

    def get(self, url, kind):
        if not self.enabled:
            return None
        with self.lock:
            row = self.conn.execute(
                "SELECT ok, ts FROM cache WHERE url=? AND kind=?",
                (url, kind)
            ).fetchone()
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
                (url, kind, int(ok), time.time())
            )
            self.conn.commit()

    def close(self):
        if self.enabled and self.conn:
            with self.lock:
                self.conn.close()


def load_sources_config(path):
    data = {'url_sources': [], 'local_sources': [], 'logo_sources': []}

    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                data.update(json.load(f))
        except Exception as e:
            log.error("Ошибка чтения %s: %s", path, e)

    env_b64 = os.environ.get('SOURCES_JSON_B64')
    if env_b64:
        try:
            decoded = base64.b64decode(env_b64).decode('utf-8')
            data.update(json.loads(decoded))
            log.info("sources.json загружен из SOURCES_JSON_B64")
        except Exception as e:
            log.warning("Ошибка декодирования SOURCES_JSON_B64: %s", e)

    return data


def download_url_sources(url_sources, cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    downloaded = []

    for src in url_sources:
        if not src.get('enabled', True):
            continue
        name = src.get('name') or f'url_{len(downloaded)}'
        url = src.get('url')
        if not url:
            continue

        out_path = os.path.join(cache_dir, f'{name}.m3u')
        try:
            r = requests.get(url, timeout=CFG.timeout, verify=CFG.verify_ssl,
                             headers={'User-Agent': DEFAULT_UA})
            r.raise_for_status()
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(r.text)
            emit(f"  OK скачан: {name} ({len(r.text)} байт)")
            log.info("Downloaded %s -> %s", url, out_path)
            downloaded.append(out_path)
        except requests.RequestException as e:
            emit(f"  FAIL не скачан: {name} ({e})")
            log.warning("Download failed %s: %s", url, e)

    return downloaded


def load_local_sources(local_sources):
    found = []
    for src in local_sources:
        if not src.get('enabled', True):
            continue
        path = src.get('path')
        if path and os.path.isfile(path):
            found.append(path)
            emit(f"  OK локальный: {path}")
        else:
            emit(f"  FAIL локальный отсутствует: {path}")
            log.warning("Local source missing: %s", path)
    return found


def split_extinf(extinf_line):
    in_quotes = False
    for i, ch in enumerate(extinf_line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ',' and not in_quotes:
            return extinf_line[:i], extinf_line[i + 1:].strip()
    return extinf_line, ''


def get_name(extinf_line):
    _, name = split_extinf(extinf_line)
    return name


def get_group(extinf_line):
    m = re.search(r'group-title="([^"]*)"', extinf_line)
    return m.group(1) if m else ''


def clean_extinf(extinf_line):
    duration_match = re.match(r'#EXTINF:\s*(-?\d+)', extinf_line)
    duration = duration_match.group(1) if duration_match else '-1'
    tvgid_match = re.search(r'tvg-id="([^"]*)"', extinf_line)
    logo_match = re.search(r'tvg-logo="([^"]*)"', extinf_line)
    group_match = re.search(r'group-title="([^"]*)"', extinf_line)
    name = get_name(extinf_line)

    parts = [f'#EXTINF:{duration}']
    if tvgid_match and tvgid_match.group(1):
        parts.append(f'tvg-id="{tvgid_match.group(1)}"')
    if logo_match and logo_match.group(1):
        parts.append(f'tvg-logo="{logo_match.group(1)}"')
    if group_match and group_match.group(1):
        parts.append(f'group-title="{group_match.group(1)}"')
    return ' '.join(parts) + ',' + name


def load_txt_logos(dirs):
    logos = {}
    txt_files = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.lower().endswith('.txt') and os.path.isfile(os.path.join(d, f)):
                txt_files.append(os.path.join(d, f))

    for path in txt_files:
        try:
            with open(path, 'r', encoding='utf-8-sig', errors='ignore') as f:
                lines = f.readlines()
        except Exception as e:
            log.warning("Ошибка чтения %s: %s", path, e)
            continue
        for line in lines:
            line = line.strip()
            if not line.startswith('#EXTINF'):
                continue
            logo_match = re.search(r'tvg-logo="([^"]*)"', line)
            if not logo_match or not logo_match.group(1):
                continue
            name = get_name(line)
            if not name:
                continue
            key = name.lower()
            if key not in logos:
                logos[key] = logo_match.group(1)

    if txt_files:
        emit(f"Логотипов из .txt: {len(logos)} (файлов: {len(txt_files)})")
    return logos


def _try_stream_with_ua(url, ua):
    headers = {'User-Agent': ua}
    try:
        with requests.get(url, headers=headers, stream=True,
                          timeout=CFG.timeout, allow_redirects=True,
                          verify=CFG.verify_ssl) as response:
            if response.status_code != 200:
                return False
            chunk = next(response.iter_content(chunk_size=512), b'')
            if not chunk:
                return False
            head = chunk.lstrip(WHITESPACE)
            if head.startswith(BOM):
                head = head[len(BOM):].lstrip(WHITESPACE)
            if head.startswith(b'#EXTM3U'):
                return True
            for prefix in HTML_PREFIXES:
                if head.startswith(prefix):
                    return False
            return True
    except requests.RequestException as e:
        log.debug("stream check failed %s: %s", url, e)
        return False


def _do_check_stream(url):
    ua_list = USER_AGENTS if CFG.multi_ua else USER_AGENTS[:1]
    for name, ua in ua_list:
        if _try_stream_with_ua(url, ua):
            return True, name
    return False, None


def check_stream(url, cache):
    cached = cache.get(url, 'stream')
    if cached is not None:
        return cached, 'cached'
    ok, ua_name = _do_check_stream(url)
    cache.put(url, 'stream', ok)
    return ok, (ua_name or DEFAULT_UA)


def _do_check_logo(url):
    headers = {'User-Agent': DEFAULT_UA}
    try:
        response = requests.head(url, headers=headers, timeout=CFG.logo_timeout,
                                 allow_redirects=True, verify=CFG.verify_ssl)
        if response.status_code in (400, 403, 405):
            with requests.get(url, headers=headers, stream=True,
                              timeout=CFG.logo_timeout,
                              allow_redirects=True, verify=CFG.verify_ssl) as r:
                return r.status_code == 200
        return response.status_code == 200
    except requests.RequestException:
        return False


def check_logo(url, cache):
    cached = cache.get(url, 'logo')
    if cached is not None:
        return cached
    ok = _do_check_logo(url)
    cache.put(url, 'logo', ok)
    return ok


def process_channel(index, extinf_line, stream_url, txt_logos, cache):
    ok, ua_used = check_stream(stream_url, cache)
    if not ok:
        return index, False, None, None, None, None

    extinf_line = clean_extinf(extinf_line)
    extras = []

    logo_match = re.search(r'tvg-logo="([^"]*)"', extinf_line)
    if logo_match and logo_match.group(1):
        logo_url = logo_match.group(1)
        if not check_logo(logo_url, cache):
            extinf_line = re.sub(r'\s?tvg-logo="[^"]*"', '', extinf_line)
            extras.append("removed broken logo")
            logo_match = None

    if not logo_match or not logo_match.group(1):
        name = get_name(extinf_line)
        found_logo = txt_logos.get(name.lower()) if name else None
        if found_logo:
            if 'tvg-id="' in extinf_line:
                extinf_line = re.sub(
                    r'(tvg-id="[^"]*")',
                    lambda m: f'{m.group(1)} tvg-logo="{found_logo}"',
                    extinf_line, count=1)
            else:
                extinf_line = re.sub(
                    r'^(#EXTINF:-?\d+)',
                    lambda m: f'{m.group(1)} tvg-logo="{found_logo}"',
                    extinf_line, count=1)
            extras.append("added logo")

    msg = '; '.join(extras) if extras else None
    return index, True, extinf_line, stream_url, msg, ua_used


def parse_playlist(filename):
    try:
        with open(filename, 'r', encoding='utf-8-sig', errors='ignore') as f:
            lines = f.readlines()
    except Exception as e:
        log.error("Ошибка чтения %s: %s", filename, e)
        return None

    if not lines or not lines[0].lstrip().startswith("#EXTM3U"):
        log.warning("Пропуск %s: нет #EXTM3U", filename)
        return None

    channels = []
    current_extinf = None
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            current_extinf = line
        elif not line.startswith("#") and current_extinf:
            channels.append((current_extinf, line))
            current_extinf = None
    return channels


def group_channels(valid_channels):
    group_order, grouped = [], {}
    for index, extinf, url in valid_channels:
        group = get_group(extinf)
        if group not in grouped:
            grouped[group] = []
            group_order.append(group)
        grouped[group].append((index, extinf, url))
    ordered = []
    for g in group_order:
        ordered.extend(grouped[g])
    return ordered


def process_playlist(filename, label, txt_logos, cache, check=True):
    emit(f"\n>>> [{label}] {filename} {'(без проверки)' if not check else ''}")
    log.info("Processing %s (check=%s)", filename, check)

    channels = parse_playlist(filename)
    if channels is None:
        return None
    if not channels:
        emit("    (каналов не найдено)")
        return {'file': filename, 'label': label, 'channels': [],
                'total': 0, 'ok': 0, 'groups': {}, 'ua_stats': {}}

    emit(f"    Каналов: {len(channels)}")

    if not check:
        out = []
        for idx, (extinf, url) in enumerate(channels):
            extinf = clean_extinf(extinf)
            out.append((idx, extinf, url))

        ordered = group_channels(out)
        group_stats = {}
        for _, extinf, _ in ordered:
            g = get_group(extinf) or '(без группы)'
            group_stats[g] = group_stats.get(g, 0) + 1

        emit(f"    Каналов добавлено (без проверки): {len(ordered)}")
        return {
            'file': filename,
            'label': label,
            'channels': [(e, u) for _, e, u in ordered],
            'total': len(ordered),
            'ok': len(ordered),
            'groups': group_stats,
            'ua_stats': {'skipped': len(ordered)},
        }

    pbar = None
    if HAS_TQDM and not CFG.quiet and not CFG.no_progress:
        pbar = tqdm(total=len(channels), desc=label, unit='ch', ncols=90, leave=True)

    valid_channels = []
    ok_count = 0
    error_count = 0
    ua_stats = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=CFG.workers) as executor:
        futures = [
            executor.submit(process_channel, i, extinf, url, txt_logos, cache)
            for i, (extinf, url) in enumerate(channels)
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                index, is_ok, extinf, url, msg, ua_used = future.result()
            except Exception as e:
                error_count += 1
                log.exception("Ошибка проверки: %s", e)
                if pbar:
                    pbar.update(1)
                continue

            if is_ok:
                valid_channels.append((index, extinf, url))
                ok_count += 1
                ua_stats[ua_used] = ua_stats.get(ua_used, 0) + 1
                log.info("[+] (%s) %s", ua_used, url)
            else:
                log.info("[-] %s", url)

            if pbar:
                pbar.update(1)
                pbar.set_postfix(ok=ok_count, err=error_count)

    if pbar:
        pbar.close()

    valid_channels.sort(key=lambda x: x[0])
    ordered = group_channels(valid_channels)

    group_stats = {}
    for _, extinf, _ in ordered:
        g = get_group(extinf) or '(без группы)'
        group_stats[g] = group_stats.get(g, 0) + 1

    emit(f"    Рабочих: {ok_count} / {len(channels)}")
    log.info("Done %s: %d/%d", label, ok_count, len(channels))

    return {
        'file': filename,
        'label': label,
        'channels': [(e, u) for _, e, u in ordered],
        'total': len(channels),
        'ok': ok_count,
        'groups': group_stats,
        'ua_stats': ua_stats,
    }


def write_playlist(path, channels):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(HEADER_LINE + "\n")
        for extinf, url in channels:
            f.write(extinf + "\n")
            f.write(UA_LINE + "\n")
            f.write(url + "\n")


REPORT_TEMPLATE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>M3U Check - отчёт {date}</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;
color:#e6e6e6;margin:0;padding:24px;max-width:1100px;margin:0 auto}}
h1{{margin:0 0 4px;font-size:22px}} h2{{font-size:15px;margin:0 0 12px}}
.sub{{color:#8a8f98;font-size:13px;margin-bottom:24px}}
a{{color:#60a5fa}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
gap:12px;margin-bottom:24px}}
.card{{background:#181b20;border:1px solid #23272e;border-radius:10px;padding:14px}}
.card .k{{font-size:11px;color:#8a8f98;text-transform:uppercase;letter-spacing:.5px}}
.card .v{{font-size:22px;font-weight:600;margin-top:6px}}
.card .v.good{{color:#4ade80}} .card .v.bad{{color:#f87171}}
.section{{background:#181b20;border:1px solid #23272e;border-radius:10px;
padding:16px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid #23272e}}
th{{color:#8a8f98;font-weight:500;font-size:11px;text-transform:uppercase}}
.bar{{background:#23272e;border-radius:4px;overflow:hidden;height:8px}}
.bar>span{{display:block;height:100%;background:#4ade80}}
.bar.mid>span{{background:#facc15}} .bar.low>span{{background:#f87171}}
.tag{{display:inline-block;padding:2px 8px;border-radius:6px;background:#23272e;
font-size:12px;color:#a1a1aa;margin:2px}}
.back{{display:inline-block;margin-bottom:16px;color:#60a5fa;text-decoration:none}}
</style></head><body>
<a class="back" href="index.html">← на главную</a>
<h1>Отчёт проверки</h1>
<div class="sub">{date} · источников: {n_playlists}</div>
<div class="cards">
<div class="card"><div class="k">Проверено</div><div class="v">{total}</div></div>
<div class="card"><div class="k">Рабочих</div><div class="v good">{ok}</div></div>
<div class="card"><div class="k">Отброшено</div><div class="v bad">{dead}</div></div>
<div class="card"><div class="k">В merged</div><div class="v">{merged}</div></div>
<div class="card"><div class="k">Время</div><div class="v">{duration}</div></div>
</div>
{ua_block}
<div class="section"><h2>По источникам</h2>
<table><thead><tr><th>Источник</th><th>Всего</th><th>Рабочих</th><th>%</th><th></th></tr></thead>
<tbody>{playlists_rows}</tbody></table></div>
<div class="section"><h2>Группы (итог)</h2>
<table><thead><tr><th>Группа</th><th>Каналов</th></tr></thead>
<tbody>{groups_rows}</tbody></table></div>
</body></html>
"""

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>IPTV - авто-обновляемый плейлист</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;
color:#e6e6e6;margin:0;padding:40px 24px;max-width:800px;margin:0 auto}}
h1{{font-size:24px;margin:0 0 8px}} h2{{font-size:15px;margin:32px 0 8px}}
.sub{{color:#8a8f98;font-size:13px;margin-bottom:24px}}
.box{{background:#181b20;border:1px solid #23272e;border-radius:10px;
padding:18px;margin-bottom:12px}}
code{{background:#0f1115;padding:3px 8px;border-radius:6px;font-size:13px;
word-break:break-all;color:#facc15}}
a{{color:#60a5fa}} .stat{{color:#8a8f98;font-size:13px}}
</style></head><body>
<h1>IPTV - авто-обновляемый плейлист</h1>
<div class="sub">Обновлено: {date}</div>

<h2>Ссылка для плеера (M3U)</h2>
<div class="box"><code>{playlist_url}</code></div>
<div class="stat">Скопируй в TiviMate / VLC / OTT Navigator как Playlist URL</div>

<h2>Отчёты</h2>
<div class="box"><a href="report.html">Открыть отчёт проверки</a></div>

<h2>Статистика</h2>
<div class="box">
Каналов в merged: <b>{merged_count}</b><br>
Рабочих источников: <b>{n_playlists}</b><br>
Рабочих каналов: <b>{ok}</b> из <b>{total}</b>
</div>
</body></html>
"""


def render_report(path, playlist_stats, merged_count, total, ok,
                  duration_sec, ua_totals):
    rows_pl = []
    for st in playlist_stats:
        pct = (st['ok'] / st['total'] * 100) if st['total'] else 0
        bar_class = '' if pct >= 70 else ('mid' if pct >= 40 else 'low')
        rows_pl.append(
            f"<tr><td>{html.escape(st['label'])}</td><td>{st['total']}</td>"
            f"<td>{st['ok']}</td><td>{pct:.1f}%</td>"
            f"<td><div class='bar {bar_class}'><span style='width:{pct:.0f}%'></span></div></td></tr>"
        )

    all_groups = {}
    for st in playlist_stats:
        for g, c in st['groups'].items():
            all_groups[g] = all_groups.get(g, 0) + c
    groups_sorted = sorted(all_groups.items(), key=lambda x: -x[1])
    rows_gr = "\n".join(
        f"<tr><td>{html.escape(g)}</td><td>{c}</td></tr>"
        for g, c in groups_sorted
    ) or "<tr><td colspan='2'>нет данных</td></tr>"

    ua_block = ''
    if ua_totals:
        tags = " ".join(
            f"<span class='tag'>{html.escape(k)}: {v}</span>"
            for k, v in sorted(ua_totals.items(), key=lambda x: -x[1])
        )
        ua_block = f"<div class='section'><h2>По User-Agent</h2><div>{tags}</div></div>"

    d = int(duration_sec)
    out = REPORT_TEMPLATE.format(
        date=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        n_playlists=len(playlist_stats),
        total=total, ok=ok, dead=total - ok, merged=merged_count,
        duration=f"{d // 60}м {d % 60}с",
        ua_block=ua_block,
        playlists_rows="\n".join(rows_pl) or "<tr><td colspan='5'>нет данных</td></tr>",
        groups_rows=rows_gr,
    )
    with open(path, 'w', encoding='utf-8') as f:
        f.write(out)


def render_index(path, playlist_url, merged_count, n_playlists, ok, total):
    out = INDEX_TEMPLATE.format(
        date=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        playlist_url=playlist_url,
        merged_count=merged_count,
        n_playlists=n_playlists,
        ok=ok, total=total,
    )
    with open(path, 'w', encoding='utf-8') as f:
        f.write(out)


def tg_send(token, chat_id, text):
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            data={'chat_id': chat_id, 'text': text,
                  'parse_mode': 'HTML', 'disable_web_page_preview': 'true'},
            timeout=15)
        if r.status_code != 200:
            log.warning("Telegram: HTTP %s: %s", r.status_code, r.text[:200])
            return False
        return True
    except requests.RequestException as e:
        log.warning("Telegram send failed: %s", e)
        return False


def tg_send_file(token, chat_id, path, caption=''):
    if not token or not chat_id or not os.path.isfile(path):
        return False
    try:
        with open(path, 'rb') as f:
            r = requests.post(
                f'https://api.telegram.org/bot{token}/sendDocument',
                data={'chat_id': chat_id, 'caption': caption[:1000]},
                files={'document': (os.path.basename(path), f, 'audio/x-mpegurl')},
                timeout=60)
        return r.status_code == 200
    except requests.RequestException as e:
        log.warning("Telegram file send failed: %s", e)
        return False


def tg_report(playlist_stats, total, ok, merged_path, duration_sec,
              report_path, index_url):
    lines = [
        "<b>M3U Check - отчёт</b>",
        f"Проверено: <b>{total}</b>",
        f"Рабочих: <b>{ok}</b> ({(ok/total*100 if total else 0):.1f}%)",
        f"Время: {int(duration_sec)}с",
        "",
    ]
    for st in playlist_stats:
        pct = (st['ok'] / st['total'] * 100) if st['total'] else 0
        lines.append(f"• {html.escape(st['label'])}: {st['ok']}/{st['total']} ({pct:.0f}%)")

    if index_url:
        lines.append("")
        lines.append(f'<a href="{index_url}">Открыть страницу</a>')

    tg_send(CFG.tg_token, CFG.tg_chat, "\n".join(lines))

    if CFG.tg_send_merged and merged_path and os.path.isfile(merged_path):
        tg_send_file(CFG.tg_token, CFG.tg_chat, merged_path,
                     caption="Объединённый плейлист (бэкап)")


def parse_args():
    p = argparse.ArgumentParser(
        description="M3U Checker v6.2",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('--sources', default='sources.json')
    p.add_argument('-o', '--output', default='checked_playlists')
    p.add_argument('--docs-dir', default='docs',
                   help='Куда писать результат для GitHub Pages')
    p.add_argument('-m', '--merged', dest='merged_name',
                   default='all_checked.m3u8',
                   help='Имя объединённого файла')
    p.add_argument('--no-merge', action='store_true')
    p.add_argument('--no-dedup', action='store_true')
    p.add_argument('-w', '--workers', type=int, default=15)
    p.add_argument('--timeout', type=float, default=5.0)
    p.add_argument('--logo-timeout', type=float, default=3.0)
    p.add_argument('--no-ssl-verify', action='store_true')
    p.add_argument('--multi-ua', action='store_true')
    p.add_argument('--cache', default='m3u_cache.sqlite')
    p.add_argument('--cache-ttl', type=int, default=3600)
    p.add_argument('--log', default='m3u_checker.log')
    p.add_argument('--no-progress', action='store_true')
    p.add_argument('-q', '--quiet', action='store_true')
    p.add_argument('--pages-url', default=os.environ.get('PAGES_URL', ''),
                   help='URL GitHub Pages для ссылки в Telegram')
    p.add_argument('--tg-token', default=os.environ.get('TG_BOT_TOKEN', ''))
    p.add_argument('--tg-chat', default=os.environ.get('TG_CHAT_ID', ''))
    p.add_argument('--tg-send-merged', action='store_true')
    return p.parse_args()


def main():
    global CFG
    CFG = parse_args()

    if CFG.no_ssl_verify:
        CFG.verify_ssl = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    else:
        CFG.verify_ssl = True

    setup_logging(CFG.log, CFG.quiet)

    emit("Загружаю конфиг источников...")
    cfg = load_sources_config(CFG.sources)

    url_sources = cfg.get('url_sources', [])
    local_sources = cfg.get('local_sources', [])
    logo_sources = cfg.get('logo_sources', [])

    cache_sources_dir = '_cache_sources'
    emit(f"\nСкачиваю {len(url_sources)} URL-источников...")
    download_url_sources(url_sources, cache_sources_dir)

    emit(f"\nПроверяю {len(local_sources)} локальных источников...")
    load_local_sources(local_sources)

    all_sources = []
    for src in url_sources:
        if not src.get('enabled', True):
            continue
        name = src.get('name') or 'url'
        path = os.path.join(cache_sources_dir, f'{name}.m3u')
        if os.path.isfile(path):
            all_sources.append((path, name, src.get('check', True)))

    for src in local_sources:
        if not src.get('enabled', True):
            continue
        path = src.get('path')
        if path and os.path.isfile(path):
            all_sources.append((path, src.get('name') or os.path.basename(path),
                                src.get('check', True)))

    if not all_sources:
        emit("\nНет доступных источников. Проверь sources.json.")
        return 1

    emit(f"\nВсего источников: {len(all_sources)}")

    logo_dirs = ['local', '.']
    for ls in logo_sources:
        if isinstance(ls, str) and os.path.isdir(ls):
            logo_dirs.append(ls)
    txt_logos = load_txt_logos(logo_dirs)

    cache = UrlCache(CFG.cache, CFG.cache_ttl)

    os.makedirs(CFG.output, exist_ok=True)
    os.makedirs(CFG.docs_dir, exist_ok=True)

    started = time.time()
    playlist_stats = []
    merged_all = []
    seen_urls = set()
    pl_order = 0
    merged_path = None

    try:
        for path, label, check in all_sources:
            stats = process_playlist(path, label, txt_logos, cache, check=check)
            if stats is None:
                continue

            safe_label = re.sub(r'[^A-Za-z0-9_.-]+', '_', label)
            out_path = os.path.join(CFG.output, f"{safe_label}.m3u8")
            write_playlist(out_path, stats['channels'])

            for extinf, url in stats['channels']:
                if not CFG.no_dedup and url in seen_urls:
                    continue
                seen_urls.add(url)
                merged_all.append((pl_order, extinf, url))

            playlist_stats.append(stats)
            pl_order += 1

        if not CFG.no_merge and merged_all:
            group_order, grouped = [], {}
            for pl, extinf, url in merged_all:
                g = get_group(extinf)
                if g not in grouped:
                    grouped[g] = []
                    group_order.append(g)
                grouped[g].append((pl, extinf, url))
            final = []
            for g in group_order:
                final.extend(grouped[g])

            merged_path = os.path.join(CFG.docs_dir, CFG.merged_name)
            write_playlist(merged_path, [(e, u) for _, e, u in final])
            write_playlist(os.path.join(CFG.output, CFG.merged_name),
                           [(e, u) for _, e, u in final])
            emit(f"\n=== ОБЪЕДИНЁННЫЙ ===")
            emit(f"    {merged_path}")
            emit(f"    Каналов: {len(final)}")

        total = sum(s['total'] for s in playlist_stats)
        ok = sum(s['ok'] for s in playlist_stats)

        report_path = os.path.join(CFG.docs_dir, 'report.html')
        ua_totals = {}
        for s in playlist_stats:
            for ua, c in s['ua_stats'].items():
                if ua == 'skipped':
                    continue
                ua_totals[ua] = ua_totals.get(ua, 0) + c
        render_report(report_path, playlist_stats, len(merged_all),
                      total, ok, time.time() - started, ua_totals)

        pages_url = CFG.pages_url.rstrip('/')
        playlist_url = f'{pages_url}/{CFG.merged_name}' if pages_url else CFG.merged_name
        index_url = f'{pages_url}/index.html' if pages_url else ''
        render_index(os.path.join(CFG.docs_dir, 'index.html'),
                     playlist_url, len(merged_all),
                     len(playlist_stats), ok, total)

        emit(f"\n=== Docs готовы ===")
        emit(f"    {CFG.docs_dir}/index.html")
        emit(f"    {CFG.docs_dir}/report.html")
        emit(f"    {CFG.docs_dir}/{CFG.merged_name}")

        if CFG.tg_token and CFG.tg_chat:
            tg_report(playlist_stats, total, ok, merged_path,
                      time.time() - started, report_path, index_url)
            emit("\nTelegram: отчёт отправлен.")
            log.info("Telegram report sent")

    finally:
        cache.close()

    total = sum(s['total'] for s in playlist_stats)
    ok = sum(s['ok'] for s in playlist_stats)
    emit(f"\nГотово за {int(time.time() - started)}с. Рабочих {ok}/{total}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
