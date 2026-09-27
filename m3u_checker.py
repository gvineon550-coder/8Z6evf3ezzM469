#!/usr/bin/env python3
"""
M3U Checker v17 — улучшенная нормализация имён, частичный матч для EPG/логотипов.
"""
import os
import re
import sys
import csv
import gzip
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
UA_ICONS = {'wink': '📺', 'vlc': '🎬', 'tivimate': '📱', 'smarttv': '📡', 'cached': '⚡'}

HEADER_LINE = (
    '#EXTM3U url-tvg="http://iptvx.one/epg/epg_lite.xml.gz; '
    'https://iptvx.one/EPG_NOARCH"'
)

IPTV_ORG_URLS_CHANNELS = [
    'https://cdn.jsdelivr.net/gh/iptv-org/api@gh-pages/channels.json',
    'https://cdn.jsdelivr.net/gh/iptv-org/api@master/channels.json',
    'https://raw.githubusercontent.com/iptv-org/api/gh-pages/channels.json',
    'https://raw.githubusercontent.com/iptv-org/api/master/channels.json',
]
IPTV_ORG_URLS_LOGOS = [
    'https://cdn.jsdelivr.net/gh/iptv-org/api@gh-pages/logos.json',
    'https://cdn.jsdelivr.net/gh/iptv-org/api@master/logos.json',
    'https://raw.githubusercontent.com/iptv-org/api/gh-pages/logos.json',
    'https://raw.githubusercontent.com/iptv-org/api/master/logos.json',
]
IPTV_ORG_CHANNELS_CACHE = '_cache_sources/iptv_org_channels.json'
IPTV_ORG_LOGOS_CACHE = '_cache_sources/iptv_org_logos.json'
IPTV_ORG_TTL_DAYS = 7

EPG_URLS = [
    'http://iptvx.one/epg/epg_lite.xml.gz',
    'https://iptvx.one/epg/epg_lite.xml.gz',
]
EPG_CACHE = 'docs/epg_map.json'
EPG_CACHE_VERSION = 17
EPG_TTL_DAYS = 7

HTML_PREFIXES = (
    b'<!DOCTYPE', b'<html', b'<HTML',
    b'<head', b'<HEAD', b'<?xml', b'<error',
)
BOM = b'\xef\xbb\xbf'
WHITESPACE = b' \t\r\n'
SLOW_THRESHOLD = 2.0
HISTORY_MAX = 30
UPTIME_MAX = 30
REJECTED_LIMIT = 3000

DONUT_COLORS = [
    '#4ade80', '#60a5fa', '#facc15', '#f87171', '#a78bfa',
    '#fb923c', '#34d399', '#f472b6', '#22d3ee', '#fbbf24',
    '#818cf8', '#fca5a5',
]

log = logging.getLogger('m3u')
CFG = None
CATEGORIES = {}
FILTERS = {}
IPTV_LOGOS = {'by_id': {}, 'by_name': {}, 'by_name_translit': {}, 'by_prefix': {}}
IPTV_IDS = {'by_name': {}, 'by_name_translit': {}, 'by_prefix': {}}
EPG_MAP = {'by_name': {}, 'by_translit': {}, 'by_prefix': {}, 'icons': {}}
LOGO_STATS = {'from_src': 0, 'from_id': 0, 'from_name': 0,
              'from_translit': 0, 'from_epg_icon': 0, 'from_prefix': 0,
              'from_txt': 0, 'none': 0}
UPTIME = {}
UPTIME_NEW = {}
UPTIME_LOCK = threading.Lock()
REJECTED = []
REJECTED_LOCK = threading.Lock()


def setup_logging(path, quiet):
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fmt_ = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S')
    fh = logging.FileHandler(path, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt_)
    log.addHandler(fh)
    if not quiet:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.WARNING)
        ch.setFormatter(fmt_)
        log.addHandler(ch)


def emit(text, pbar=None):
    if CFG.quiet:
        return
    if pbar is not None and HAS_TQDM:
        tqdm.write(text)
    else:
        print(text)


def fmt(template, **kwargs):
    out = template.replace('{{', '\x00').replace('}}', '\x01')
    for k, v in kwargs.items():
        out = out.replace('{' + k + '}', str(v))
    return out.replace('\x00', '{').replace('\x01', '}')


_TRANSLIT_MAP = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh',
    'з':'z','и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o',
    'п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'ts',
    'ч':'ch','ш':'sh','щ':'sch','ъ':'','ы':'y','ь':'','э':'e',
    'ю':'yu','я':'ya','і':'i','ї':'i','є':'e','ґ':'g',
}

# Слова-шум: убираются из имён
NOISE_WORDS = re.compile(
    r'\b(тв|tv|телеканал|channel|канал|hd|fhd|uhd|4k|8k|sd|hevc|h265|h264|mp4|hq|lq|'
    r'ру|ru|россия|russia|online|live)\b',
    re.IGNORECASE,
)

ROMAN_TAIL = re.compile(r'\b(i{1,3}|iv|v|vi{1,3}|ix|x)\s*$', re.IGNORECASE)


def normalize_name_v2(name):
    """Улучшенная нормализация для матчинга логотипов/EPG.
    
    - Lowercase
    - Замена ё→е, й→и (для унификации)
    - Убираем содержимое скобок (...), [...], <...>
    - Заменяем все спецсимволы на пробел
    - Убираем слова-шум (ТВ, TV, канал, HD...)
    - Убираем римские цифры в конце
    - Схлопываем пробелы
    """
    if not name:
        return ''
    n = name.lower().strip()
    # Замена ё→е, й→и
    n = n.replace('ё', 'е').replace('й', 'и')
    # Убираем содержимое скобок
    n = re.sub(r'\([^)]*\)', ' ', n)
    n = re.sub(r'\[[^\]]*\]', ' ', n)
    n = re.sub(r'<[^>]*>', ' ', n)
    # Спецсимволы -> пробел
    n = re.sub(r'[^a-zа-я0-9]+', ' ', n)
    # Убираем слова-шум
    n = NOISE_WORDS.sub(' ', n)
    # Убираем римские цифры в конце
    n = ROMAN_TAIL.sub('', n)
    # Схлопываем пробелы
    return re.sub(r'\s+', ' ', n).strip()


def normalize_name(name):
    """Совместимость со старым вызовом."""
    return normalize_name_v2(name)


def normalize_name_translit(name):
    n = normalize_name_v2(name)
    return translit_ru(n) if n else ''


def make_prefix(normalized):
    """Первые 5 символов нормализованного имени (без пробелов)."""
    if not normalized:
        return ''
    s = normalized.replace(' ', '')
    return s[:5] if len(s) >= 5 else ''


def translit_ru(s):
    if not s:
        return ''
    return ''.join(_TRANSLIT_MAP.get(ch, ch) for ch in s.lower())


# ---------- UPTIME ----------
def load_uptime(path):
    global UPTIME
    if not os.path.isfile(path):
        UPTIME = {}
        return
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        UPTIME = {k: v for k, v in data.items() if isinstance(v, list)} if isinstance(data, dict) else {}
    except Exception as e:
        log.warning("uptime.json: %s", e)
        UPTIME = {}


def save_uptime(path, current_urls):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with UPTIME_LOCK:
        for url, val in UPTIME_NEW.items():
            hist = UPTIME.get(url, [])
            hist.append(val)
            UPTIME[url] = hist[-UPTIME_MAX:]
        alive = set(current_urls)
        for url in list(UPTIME.keys()):
            if url not in alive:
                del UPTIME[url]
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(UPTIME, f, ensure_ascii=False, separators=(',', ':'))


def get_uptime_pct(url):
    hist = UPTIME.get(url)
    if not hist:
        return None, 0
    total = len(hist)
    if total == 0:
        return None, 0
    return round(sum(hist) / total * 100), total


def is_migayushchiy(url):
    if CFG.min_uptime <= 0:
        return False
    pct, total = get_uptime_pct(url)
    if pct is None or total < CFG.min_uptime_samples:
        return False
    return pct < CFG.min_uptime


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
            row = self.conn.execute("SELECT ok, ts FROM cache WHERE url=? AND kind=?",
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


# ---------- IPTV-ORG ----------
def _download_json(urls, cache_path):
    os.makedirs(os.path.dirname(cache_path) or '.', exist_ok=True)
    fresh = False
    if os.path.isfile(cache_path):
        age = (time.time() - os.path.getmtime(cache_path)) / 86400
        fresh = age < IPTV_ORG_TTL_DAYS
    if not fresh:
        for url in urls:
            try:
                r = requests.get(url, timeout=60, verify=CFG.verify_ssl,
                                 headers={'User-Agent': DEFAULT_UA})
                if r.status_code != 200:
                    continue
                data = r.json()
                if not isinstance(data, list) or not data:
                    continue
                with open(cache_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False)
                emit(f"  Скачано: {url.split('/')[-1]} ({len(data)} записей)")
                return data
            except Exception as e:
                log.debug("%s: %s", url, e)
        if os.path.isfile(cache_path):
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return None
        return None
    with open(cache_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _add_to_prefix_map(prefix_map, prefix, value):
    """Добавляет значение в список по префиксу."""
    if not prefix:
        return
    prefix_map.setdefault(prefix, []).append(value)


def load_iptv_org():
    global IPTV_LOGOS, IPTV_IDS
    if CFG.no_iptv_logos:
        emit("База iptv-org: отключено")
        return
    emit("  Скачиваю каналы...")
    channels = _download_json(IPTV_ORG_URLS_CHANNELS, IPTV_ORG_CHANNELS_CACHE)
    if not channels:
        emit("  Каналы iptv-org недоступны")
        return
    emit("  Скачиваю логотипы...")
    logos = _download_json(IPTV_ORG_URLS_LOGOS, IPTV_ORG_LOGOS_CACHE)
    if not logos:
        emit("  Логотипы iptv-org недоступны")
        logos = []

    id_to_names = {}
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        cid = ch.get('id')
        if not cid:
            continue
        names = []
        n = ch.get('name')
        if isinstance(n, str):
            names.append(n)
        an = ch.get('alt_names')
        if isinstance(an, list):
            names.extend([x for x in an if isinstance(x, str)])
        elif isinstance(an, str):
            names.append(an)
        id_to_names[cid] = names

    by_id_raw = {}
    for logo in logos:
        if not isinstance(logo, dict):
            continue
        cid = logo.get('channel')
        url = logo.get('url') or logo.get('logo')
        if not cid or not url:
            continue
        w = logo.get('width') or 0
        cur = by_id_raw.get(cid)
        if cur is None or (w and cur[1] < w):
            by_id_raw[cid] = (url, w)
    by_id = {k: v[0] for k, v in by_id_raw.items()}

    by_name_logo = {}
    by_name_logo_translit = {}
    by_prefix_logo = {}
    for cid, url in by_id.items():
        for name in id_to_names.get(cid, []):
            k = normalize_name_v2(name)
            if k and k not in by_name_logo:
                by_name_logo[k] = url
            kt = normalize_name_translit(name)
            if kt and kt not in by_name_logo_translit:
                by_name_logo_translit[kt] = url
            p = make_prefix(k)
            _add_to_prefix_map(by_prefix_logo, p, (k, url))

    IPTV_LOGOS['by_id'] = by_id
    IPTV_LOGOS['by_name'] = by_name_logo
    IPTV_LOGOS['by_name_translit'] = by_name_logo_translit
    IPTV_LOGOS['by_prefix'] = by_prefix_logo

    by_name_id = {}
    by_name_id_translit = {}
    by_prefix_id = {}
    for cid, names in id_to_names.items():
        for name in names:
            k = normalize_name_v2(name)
            if k and k not in by_name_id:
                by_name_id[k] = cid
            kt = normalize_name_translit(name)
            if kt and kt not in by_name_id_translit:
                by_name_id_translit[kt] = cid
            p = make_prefix(k)
            _add_to_prefix_map(by_prefix_id, p, (k, cid))

    IPTV_IDS['by_name'] = by_name_id
    IPTV_IDS['by_name_translit'] = by_name_id_translit
    IPTV_IDS['by_prefix'] = by_prefix_id

    emit(f"  Логотипов: {len(by_id)} по id, {len(by_name_logo)} по имени, "
         f"{len(by_prefix_logo)} префиксов")
    emit(f"  iptv-org id: {len(by_name_id)} по имени, {len(by_prefix_id)} префиксов")


# ---------- EPG iptvx ----------
def download_epg_map():
    global EPG_MAP
    if os.path.isfile(EPG_CACHE):
        try:
            with open(EPG_CACHE, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            cached_ver = cached.get('version', 0)
            age_days = (time.time() - cached.get('ts', 0)) / 86400
            if cached_ver == EPG_CACHE_VERSION and age_days < EPG_TTL_DAYS:
                EPG_MAP['by_name'] = cached.get('by_name', {})
                EPG_MAP['by_translit'] = cached.get('by_translit', {})
                EPG_MAP['by_prefix'] = cached.get('by_prefix', {})
                EPG_MAP['icons'] = cached.get('icons', {})
                emit(f"  EPG: из кэша v{cached_ver} {len(EPG_MAP['by_name'])} имён, "
                     f"{len(EPG_MAP['icons'])} icon")
                return
            else:
                emit(f"  EPG: кэш устарел (v{cached_ver}), пересобираю...")
        except Exception as e:
            log.warning("EPG cache: %s", e)

    for url in EPG_URLS:
        try:
            emit(f"  EPG: скачиваю {url}")
            r = requests.get(url, timeout=180, verify=CFG.verify_ssl,
                             headers={'User-Agent': DEFAULT_UA})
            if r.status_code != 200:
                continue
            raw = r.content
            try:
                xml_data = gzip.decompress(raw)
            except Exception:
                xml_data = raw
            emit(f"  EPG: распакован {len(xml_data)//1024} KB, парсим...")

            head = xml_data[:200].decode('ascii', errors='ignore')
            if 'windows-1251' in head or 'cp1251' in head:
                xml_str = xml_data.decode('windows-1251', errors='replace')
            else:
                xml_str = xml_data.decode('utf-8', errors='replace')

            ch_re = re.compile(r'<channel\s+id="([^"]+)"[^>]*>(.*?)</channel>',
                               re.DOTALL)
            dn_re = re.compile(r'<display-name[^>]*>([^<]+)</display-name>')
            icon_re = re.compile(r'<icon\s+src="([^"]+)"')

            by_name = {}
            by_translit = {}
            by_prefix = {}
            icons = {}
            for m in ch_re.finditer(xml_str):
                cid = m.group(1)
                body = m.group(2)
                ic = icon_re.search(body)
                if ic:
                    icons[cid] = ic.group(1)
                for dn in dn_re.finditer(body):
                    name = html.unescape(dn.group(1)).strip()
                    if not name:
                        continue
                    k = normalize_name_v2(name)
                    if k and k not in by_name:
                        by_name[k] = cid
                    kt = normalize_name_translit(name)
                    if kt and kt not in by_translit:
                        by_translit[kt] = cid
                    p = make_prefix(k)
                    _add_to_prefix_map(by_prefix, p, (k, cid))

            EPG_MAP['by_name'] = by_name
            EPG_MAP['by_translit'] = by_translit
            EPG_MAP['by_prefix'] = by_prefix
            EPG_MAP['icons'] = icons
            emit(f"  EPG: {len(by_name)} имён, {len(by_translit)} с транслитом, "
                 f"{len(icons)} icon, {len(by_prefix)} префиксов")

            os.makedirs(os.path.dirname(EPG_CACHE) or '.', exist_ok=True)
            with open(EPG_CACHE, 'w', encoding='utf-8') as f:
                json.dump({'version': EPG_CACHE_VERSION,
                           'by_name': by_name, 'by_translit': by_translit,
                           'by_prefix': by_prefix, 'icons': icons,
                           'ts': time.time()},
                          f, ensure_ascii=False, separators=(',', ':'))
            return
        except Exception as e:
            log.warning("EPG %s: %s", url, e)
    emit("  EPG: не удалось скачать, tvg-id не трогаем")


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


def get_tvg_id(line):
    m = re.search(r'tvg-id="([^"]*)"', line)
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


def set_tvg_id_in_extinf(extinf, tvg_id):
    if not tvg_id:
        return remove_tvg_id_from_extinf(extinf)
    tvg_id = tvg_id.replace('"', "'")
    if re.search(r'tvg-id="[^"]*"', extinf):
        return re.sub(r'tvg-id="[^"]*"', f'tvg-id="{tvg_id}"', extinf)
    return re.sub(r'^(#EXTINF:-?\d+)', rf'\1 tvg-id="{tvg_id}"', extinf, count=1)


def remove_tvg_id_from_extinf(extinf):
    return re.sub(r'\s?tvg-id="[^"]*"', '', extinf, count=1)


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


# ---------- ПОТОКИ ----------
def _try_stream(url, ua):
    """Возвращает (ok, elapsed, reason)."""
    try:
        t0 = time.time()
        with requests.get(url, headers={'User-Agent': ua}, stream=True,
                          timeout=CFG.timeout, allow_redirects=True,
                          verify=CFG.verify_ssl) as r:
            elapsed = time.time() - t0
            if r.status_code != 200:
                return False, elapsed, f'http_{r.status_code}'
            ct = (r.headers.get('Content-Type') or '').lower()
            chunk = next(r.iter_content(chunk_size=1024), b'')
            if not chunk:
                return False, elapsed, 'empty'
            h = chunk.lstrip(WHITESPACE)
            if h.startswith(BOM):
                h = h[len(BOM):].lstrip(WHITESPACE)
            if h.startswith(b'#EXTM3U'):
                return True, elapsed, None
            for p in HTML_PREFIXES:
                if h.startswith(p):
                    return False, elapsed, 'html'
            if len(chunk) < 32:
                return False, elapsed, 'too_small'
            # Ослаблено: если Content-Type text/html, но НЕ начинается с HTML,
            # значит это скорее всего видео с криво настроенным сервером
            return True, elapsed, None
    except requests.RequestException:
        return False, None, 'timeout'


def _check_stream(url):
    uas = USER_AGENTS if CFG.multi_ua else USER_AGENTS[:1]
    best_elapsed = None
    last_reason = 'unknown'
    for name, ua in uas:
        ok, el, reason = _try_stream(url, ua)
        if ok:
            return True, name, el, None
        if reason:
            last_reason = reason
        if el is not None and (best_elapsed is None or el < best_elapsed):
            best_elapsed = el
    return False, None, best_elapsed, last_reason


def check_stream(url, cache):
    c = cache.get(url, 'stream')
    if c is not None:
        return c, 'cached', None, None
    ok, n, el, reason = _check_stream(url)
    cache.put(url, 'stream', ok)
    return ok, (n or DEFAULT_UA), el, reason


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


def _pick_prefix_match(prefix_map, prefix):
    """Возвращает значение, если в prefix_map[prefix] уникальное имя (не более 3)."""
    if not prefix:
        return None
    items = prefix_map.get(prefix)
    if not items:
        return None
    # Ограничиваемся списком до 3, чтобы не путать разные каналы
    if len(items) > 3:
        return None
    # Берём первый по алфавиту имени (детерминированно)
    items_sorted = sorted(items, key=lambda x: x[0])
    return items_sorted[0][1]


def resolve_logo(extinf, url, txt_logos, cache, iptv_org_id=None, epg_id=None):
    m = re.search(r'tvg-logo="([^"]*)"', extinf)
    if m and m.group(1):
        if not CFG.check_all_logos or check_logo(m.group(1), cache):
            return m.group(1), 'from_src'

    if iptv_org_id:
        l = IPTV_LOGOS['by_id'].get(iptv_org_id)
        if l:
            return l, 'from_id'

    name = get_name(extinf)
    if name:
        k = normalize_name_v2(name)
        if k:
            l = IPTV_LOGOS['by_name'].get(k)
            if l:
                return l, 'from_name'
        kt = normalize_name_translit(name)
        if kt:
            l = IPTV_LOGOS.get('by_name_translit', {}).get(kt)
            if l:
                return l, 'from_translit'
        # Частичный матч по префиксу (5 символов)
        if k:
            p = make_prefix(k)
            l = _pick_prefix_match(IPTV_LOGOS.get('by_prefix', {}), p)
            if l:
                return l, 'from_prefix'

    if epg_id and EPG_MAP.get('icons'):
        l = EPG_MAP['icons'].get(epg_id)
        if l:
            return l, 'from_epg_icon'

    if name:
        l = txt_logos.get(name.lower())
        if l and check_logo(l, cache):
            return l, 'from_txt'

    return None, 'none'


def resolve_iptv_org_id(extinf, original_tvg_id):
    if original_tvg_id and original_tvg_id in IPTV_LOGOS['by_id']:
        return original_tvg_id
    name = get_name(extinf)
    if not name:
        return None
    k = normalize_name_v2(name)
    if k:
        cid = IPTV_IDS['by_name'].get(k)
        if cid:
            return cid
    kt = normalize_name_translit(name)
    if kt:
        cid = IPTV_IDS['by_name_translit'].get(kt)
        if cid:
            return cid
    if k:
        p = make_prefix(k)
        cid = _pick_prefix_match(IPTV_IDS.get('by_prefix', {}), p)
        if cid:
            return cid
    return None


def resolve_epg_id(extinf):
    if not EPG_MAP['by_name'] and not EPG_MAP['by_translit']:
        return None
    name = get_name(extinf)
    if not name:
        return None
    k = normalize_name_v2(name)
    if k:
        cid = EPG_MAP['by_name'].get(k)
        if cid:
            return cid
    kt = normalize_name_translit(name)
    if kt:
        cid = EPG_MAP['by_translit'].get(kt)
        if cid:
            return cid
    if k:
        p = make_prefix(k)
        cid = _pick_prefix_match(EPG_MAP.get('by_prefix', {}), p)
        if cid:
            return cid
    return None


def process_channel(index, extinf, url, txt_logos, cache, source_name):
    name = get_name(extinf)
    group = get_group(extinf)
    filtered, reason = is_filtered(name, group, url)
    if filtered:
        with REJECTED_LOCK:
            if len(REJECTED) < REJECTED_LIMIT:
                REJECTED.append((name, group, url, reason))
        return index, 'filtered', None, None, reason, None, None, None, None

    ok, ua, elapsed, fail_reason = check_stream(url, cache)
    if not ok:
        with UPTIME_LOCK:
            UPTIME_NEW[url] = 0
        with REJECTED_LOCK:
            if len(REJECTED) < REJECTED_LIMIT:
                REJECTED.append((name, group, url, f'dead:{fail_reason or "unknown"}'))
        return index, 'dead', None, None, None, None, None, None, None

    with UPTIME_LOCK:
        UPTIME_NEW[url] = 1

    if is_migayushchiy(url):
        pct, total = get_uptime_pct(url)
        with REJECTED_LOCK:
            if len(REJECTED) < REJECTED_LIMIT:
                REJECTED.append((name, group, url, f'unstable:{pct}%'))
        return index, 'unstable', None, None, f"uptime {pct}%", None, None, None, None

    extinf = clean_extinf(extinf)

    original_tvg_id = get_tvg_id(extinf)
    iptv_org_id = resolve_iptv_org_id(extinf, original_tvg_id)
    epg_id = resolve_epg_id(extinf)

    logo, logo_src = resolve_logo(extinf, url, txt_logos, cache,
                                   iptv_org_id, epg_id)
    extinf = set_logo_in_extinf(extinf, logo)

    if epg_id:
        extinf = set_tvg_id_in_extinf(extinf, epg_id)
    else:
        extinf = remove_tvg_id_from_extinf(extinf)

    new_group = categorize(extinf, url, source_name)
    extinf = set_group_in_extinf(extinf, add_emoji(new_group))

    return index, 'ok', extinf, url, None, ua, new_group, elapsed, logo_src


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
                'filtered': 0, 'unstable': 0, 'groups': {}, 'ua_stats': {},
                'slow': [], 'fastest': None, 'avg_elapsed': None,
                'logo_stats': {}, 'all_urls': []}

    emit(f"    Каналов: {len(channels)}")

    if not check:
        out, all_urls = [], []
        for i, (extinf, url) in enumerate(channels):
            all_urls.append(url)
            name = get_name(extinf)
            group = get_group(extinf)
            filtered, reason = is_filtered(name, group, url)
            if filtered:
                with REJECTED_LOCK:
                    if len(REJECTED) < REJECTED_LIMIT:
                        REJECTED.append((name, group, url, reason))
                continue
            extinf = clean_extinf(extinf)
            original_tvg_id = get_tvg_id(extinf)
            iptv_org_id = resolve_iptv_org_id(extinf, original_tvg_id)
            epg_id = resolve_epg_id(extinf)
            logo, _ = resolve_logo(extinf, url, txt_logos, cache, iptv_org_id, epg_id)
            extinf = set_logo_in_extinf(extinf, logo)
            if epg_id:
                extinf = set_tvg_id_in_extinf(extinf, epg_id)
            else:
                extinf = remove_tvg_id_from_extinf(extinf)
            extinf = set_group_in_extinf(extinf, add_emoji(categorize(extinf, url, label)))
            out.append((i, extinf, url))
        ordered = group_channels(out)
        gs = {}
        for _, e, _ in ordered:
            g = get_group(e) or '(без группы)'
            gs[g] = gs.get(g, 0) + 1
        return {'label': label, 'channels': [(e, u) for _, e, u in ordered],
                'total': len(channels), 'ok': len(ordered),
                'filtered': len(channels) - len(ordered), 'unstable': 0,
                'groups': gs, 'ua_stats': {'skipped': len(ordered)},
                'slow': [], 'fastest': None, 'avg_elapsed': None,
                'logo_stats': {}, 'all_urls': all_urls}

    pbar = None
    if HAS_TQDM and not CFG.quiet and not CFG.no_progress:
        pbar = tqdm(total=len(channels), desc=label, unit='ch', ncols=90, leave=True)

    valid, ok_cnt, err_cnt, filt_cnt, unstable_cnt = [], 0, 0, 0, 0
    ua_stats, filt_by_reason = {}, {}
    slow_list, elapsed_list = [], []
    logo_counts = {}
    all_urls = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=CFG.workers) as ex:
        futs = [ex.submit(process_channel, i, e, u, txt_logos, cache, label)
                for i, (e, u) in enumerate(channels)]
        for fu in concurrent.futures.as_completed(futs):
            try:
                idx, status, extinf, url, msg, ua, grp, el, logo_src = fu.result()
            except Exception as e:
                err_cnt += 1
                log.exception("Ошибка: %s", e)
                if pbar:
                    pbar.update(1)
                continue
            all_urls.append(channels[idx][1] if idx < len(channels) else '')
            if status == 'ok':
                valid.append((idx, extinf, url))
                ok_cnt += 1
                ua_stats[ua] = ua_stats.get(ua, 0) + 1
                if logo_src:
                    logo_counts[logo_src] = logo_counts.get(logo_src, 0) + 1
                if el is not None:
                    elapsed_list.append(el)
                    if el >= SLOW_THRESHOLD:
                        slow_list.append((get_name(extinf), url, round(el, 2)))
            elif status == 'filtered':
                filt_cnt += 1
                filt_by_reason[msg] = filt_by_reason.get(msg, 0) + 1
            elif status == 'unstable':
                unstable_cnt += 1
                filt_by_reason['unstable'] = filt_by_reason.get('unstable', 0) + 1
            if pbar:
                pbar.update(1)
                pbar.set_postfix(ok=ok_cnt, filt=filt_cnt, unst=unstable_cnt, err=err_cnt)
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

    emit(f"    Рабочих: {ok_cnt}, фильтр: {filt_cnt}, нестабильных: {unstable_cnt}, ошибок: {err_cnt}")
    if logo_counts:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(logo_counts.items(), key=lambda x: -x[1]))
        emit(f"    Логотипы: {parts}")
        for k, v in logo_counts.items():
            LOGO_STATS[k] = LOGO_STATS.get(k, 0) + v

    return {'label': label, 'channels': [(e, u) for _, e, u in ordered],
            'total': len(channels), 'ok': ok_cnt, 'filtered': filt_cnt,
            'unstable': unstable_cnt,
            'groups': gs, 'ua_stats': ua_stats, 'filter_reasons': filt_by_reason,
            'slow': slow_list, 'fastest': fastest, 'avg_elapsed': avg_el,
            'logo_stats': logo_counts, 'all_urls': all_urls}


def dedup_by_name_fn(channels):
    seen, out = set(), []
    for extinf, url in channels:
        key = normalize_name_v2(get_name(extinf))
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
    s = re.sub(r'^\S+\s+', '', g).lower()
    out = []
    for ch in s:
        if ch in _TRANSLIT_MAP:
            out.append(_TRANSLIT_MAP[ch])
        elif ch.isalnum():
            out.append(ch)
        else:
            out.append('_')
    return re.sub(r'_+', '_', ''.join(out)).strip('_')


def write_splits(channels, docs_dir, split_all=False):
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
        w.writerow(['name', 'group', 'tvg_id', 'uptime_pct', 'uptime_samples', 'logo', 'url'])
        for extinf, url in channels:
            name = get_name(extinf)
            group = get_group(extinf)
            tid = get_tvg_id(extinf)
            pct, total = get_uptime_pct(url)
            pct_v = pct if pct is not None else ''
            m = re.search(r'tvg-logo="([^"]*)"', extinf)
            logo = m.group(1) if m else ''
            w.writerow([name, group, tid, pct_v, total, logo, url])


def write_rejected_csv(path):
    seen = set()
    rows = []
    for name, group, url, reason in REJECTED:
        key = (url, reason)
        if key in seen:
            continue
        seen.add(key)
        rows.append((name, group, url, reason))
    rows.sort(key=lambda x: (x[3], x[1].lower(), x[0].lower()))
    with open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['name', 'group', 'url', 'reason'])
        for row in rows:
            w.writerow(row)
    return len(rows)


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


def sparkline_svg(vals, color='#4ade80', w=100, h=24):
    if not vals or len(vals) < 2:
        return ''
    mx = max(vals) or 1
    n = len(vals)
    step = w / max(n - 1, 1)
    pts = []
    for i, v in enumerate(vals):
        x = i * step
        y = h - (v / mx) * (h - 2) - 1
        pts.append(f"{x:.1f},{y:.1f}")
    poly = " ".join(pts)
    area = f"M0,{h} L" + " L".join(pts) + f" L{w},{h} Z"
    return (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
            f'style="width:100%;height:{h}px;display:block;margin-top:8px">'
            f'<path d="{area}" fill="{color}" fill-opacity="0.15"/>'
            f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1.5"/></svg>')


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


def render_donut_svg(groups_dict):
    if not groups_dict:
        return ''
    items = sorted(groups_dict.items(), key=lambda x: -x[1])
    top = items[:10]
    rest = sum(c for _, c in items[10:])
    if rest:
        top.append(('Прочее', rest))
    total = sum(c for _, c in top) or 1
    cx, cy, R, SW = 110, 110, 80, 22
    C = 2 * 3.14159265 * R
    svg_parts = [f'<circle cx="{cx}" cy="{cy}" r="{R}" fill="none" '
                 f'stroke="var(--border)" stroke-width="{SW}"/>']
    offset = 0
    legend = []
    for i, (name, count) in enumerate(top):
        frac = count / total
        dash = frac * C
        color = DONUT_COLORS[i % len(DONUT_COLORS)]
        svg_parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{R}" fill="none" '
            f'stroke="{color}" stroke-width="{SW}" '
            f'stroke-dasharray="{dash:.2f} {C - dash:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 {cx} {cy})"/>'
        )
        offset += dash
        legend.append(
            f'<div class="legend-row">'
            f'<span class="legend-dot" style="background:{color}"></span>'
            f'<span class="legend-name">{html.escape(name)}</span>'
            f'<span class="legend-count">{count}</span>'
            f'<span class="legend-pct">{frac*100:.1f}%</span></div>'
        )
    total_label = f'<text x="{cx}" y="{cy-4}" text-anchor="middle" fill="var(--text)" font-size="20" font-weight="600">{total}</text>'
    total_sub = f'<text x="{cx}" y="{cy+14}" text-anchor="middle" fill="var(--muted)" font-size="10">КАНАЛОВ</text>'
    svg = (f'<svg viewBox="0 0 {cx*2} {cy*2}" style="max-width:220px;width:100%">'
           + "".join(svg_parts) + total_label + total_sub + '</svg>')
    return f'''<div class="section"><h2>Распределение по группам</h2>
<div class="donut-wrap">
<div class="donut-chart">{svg}</div>
<div class="donut-legend">{"".join(legend)}</div>
</div></div>'''


def health_bar(pct):
    color = '#4ade80' if pct >= 70 else ('#facc15' if pct >= 40 else '#f87171')
    return f'''<div class="health-wrap">
<div class="health-bar"><div style="width:{pct:.1f}%;background:{color}"></div></div>
<div class="health-label">Здоровье системы: <b style="color:{color}">{pct:.0f}%</b></div>
</div>'''


COMMON_CSS = """
*{box-sizing:border-box}
:root{
  --bg:#0f1115; --panel:rgba(24,27,32,0.72); --border:#23272e;
  --text:#e6e6e6; --muted:#8a8f98; --accent:#60a5fa;
  --glass-blur:20px;
}
[data-theme="light"]{
  --bg:#f6f7f9; --panel:rgba(255,255,255,0.75); --border:#e4e6ea;
  --text:#1a1d21; --muted:#6b7280; --accent:#2563eb;
}
html{min-height:100%}
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;
color:var(--text);margin:0;padding:24px;max-width:1100px;margin:0 auto;
min-height:100vh;
background:var(--bg);
background-image:
  radial-gradient(circle at 15% -10%, rgba(96,165,250,0.12) 0%, transparent 40%),
  radial-gradient(circle at 85% 0%, rgba(74,222,128,0.10) 0%, transparent 40%),
  radial-gradient(circle at 50% 100%, rgba(167,139,250,0.06) 0%, transparent 50%);
background-attachment:fixed;
transition:background-color .3s,color .3s}
h1{margin:0 0 4px;font-size:22px;letter-spacing:-.3px}
h2{font-size:15px;margin:0 0 12px;letter-spacing:-.1px}
.sub{color:var(--muted);font-size:13px;margin-bottom:24px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
gap:12px;margin-bottom:24px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:14px;
padding:16px;backdrop-filter:blur(var(--glass-blur));
-webkit-backdrop-filter:blur(var(--glass-blur));
transition:transform .15s,border-color .15s,box-shadow .15s}
.card:hover{transform:translateY(-1px);border-color:var(--accent)}
.card .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.card .v{font-size:26px;font-weight:700;margin-top:6px;letter-spacing:-.5px;
font-variant-numeric:tabular-nums}
.card .v.good{color:#4ade80;text-shadow:0 0 24px rgba(74,222,128,0.25)}
.card .v.bad{color:#f87171}
.card .v.warn{color:#facc15}
.section{background:var(--panel);border:1px solid var(--border);border-radius:14px;
padding:18px;margin-bottom:16px;backdrop-filter:blur(var(--glass-blur));
-webkit-backdrop-filter:blur(var(--glass-blur))}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border)}
th{color:var(--muted);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
tr:last-child td{border-bottom:none}
.tag{display:inline-block;padding:3px 10px;border-radius:20px;background:var(--border);
font-size:12px;color:var(--muted);margin:2px}
.back{display:inline-block;margin-bottom:16px;color:var(--accent);text-decoration:none}
.box{background:var(--panel);border:1px solid var(--border);border-radius:14px;
padding:18px;margin-bottom:12px;backdrop-filter:blur(var(--glass-blur));
-webkit-backdrop-filter:blur(var(--glass-blur))}
code{background:var(--bg);padding:3px 8px;border-radius:6px;font-size:13px;
word-break:break-all;color:#facc15;font-family:ui-monospace,Menlo,monospace}
.theme-btn{position:fixed;top:16px;right:16px;background:var(--panel);
border:1px solid var(--border);color:var(--text);width:42px;height:42px;
border-radius:50%;cursor:pointer;font-size:18px;z-index:100;line-height:1;
backdrop-filter:blur(var(--glass-blur));
-webkit-backdrop-filter:blur(var(--glass-blur));
transition:transform .15s}
.theme-btn:hover{transform:scale(1.08)}
.health-wrap{margin-bottom:24px;padding:14px 18px;background:var(--panel);
border:1px solid var(--border);border-radius:14px;
backdrop-filter:blur(var(--glass-blur));
-webkit-backdrop-filter:blur(var(--glass-blur))}
.health-bar{height:8px;background:var(--border);border-radius:8px;overflow:hidden}
.health-bar > div{height:100%;border-radius:8px;transition:width .8s cubic-bezier(.4,0,.2,1)}
.health-label{margin-top:8px;font-size:13px;color:var(--muted)}
.donut-wrap{display:flex;gap:24px;align-items:center;flex-wrap:wrap}
.donut-chart{flex:0 0 220px}
.donut-legend{flex:1;min-width:240px}
.legend-row{display:flex;align-items:center;gap:8px;padding:5px 0;font-size:13px;
border-bottom:1px solid var(--border)}
.legend-row:last-child{border-bottom:none}
.legend-dot{width:10px;height:10px;border-radius:3px;flex:0 0 auto}
.legend-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.legend-count{font-variant-numeric:tabular-nums;color:var(--muted);font-size:12px}
.legend-pct{font-variant-numeric:tabular-nums;color:var(--text);font-size:12px;
font-weight:500;min-width:44px;text-align:right}
.wf-status{font-size:13px;margin-bottom:16px;padding:10px 14px;
background:var(--panel);border:1px solid var(--border);border-radius:12px;
backdrop-filter:blur(var(--glass-blur));
-webkit-backdrop-filter:blur(var(--glass-blur))}
.copy-btn{background:var(--border);border:none;color:var(--text);padding:6px 12px;
border-radius:8px;font-size:12px;cursor:pointer;transition:background .15s}
.copy-btn:hover{background:var(--accent);color:#fff}
.copy-btn.copied{background:#4ade80;color:#0f1115}
.uptime-good{color:#4ade80;font-weight:500}
.uptime-mid{color:#facc15;font-weight:500}
.uptime-low{color:#f87171;font-weight:500}
.uptime-none{color:var(--muted)}
.reason-badge{display:inline-block;padding:2px 8px;border-radius:6px;
font-size:11px;font-weight:500;background:var(--border);color:var(--muted)}
.reason-badge.dead{background:rgba(248,113,113,0.15);color:#f87171}
.reason-badge.filter{background:rgba(250,204,21,0.15);color:#facc15}
.reason-badge.unstable{background:rgba(250,204,21,0.15);color:#facc15}
"""

THEME_JS = """
<script>
(function(){
  const saved = localStorage.getItem('theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  function animateCounters(){
    document.querySelectorAll('[data-count]').forEach(el => {
      const target = parseInt(el.dataset.count, 10);
      if (isNaN(target)) return;
      const dur = 900; const t0 = performance.now();
      function step(t){
        const p = Math.min((t - t0) / dur, 1);
        const eased = 1 - Math.pow(1 - p, 3);
        el.textContent = Math.floor(target * eased).toLocaleString('ru-RU');
        if (p < 1) requestAnimationFrame(step);
        else el.textContent = target.toLocaleString('ru-RU');
      }
      requestAnimationFrame(step);
    });
  }
  document.addEventListener('DOMContentLoaded', function(){
    const btn = document.querySelector('.theme-btn');
    if(btn){
      btn.textContent = saved === 'dark' ? '☀' : '☾';
      btn.addEventListener('click', function(){
        const cur = document.documentElement.getAttribute('data-theme');
        const nxt = cur === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', nxt);
        localStorage.setItem('theme', nxt);
        btn.textContent = nxt === 'dark' ? '☀' : '☾';
      });
    }
    animateCounters();
  });
})();
</script>
"""

THEME_BTN = '<button class="theme-btn" aria-label="Theme">☀</button>'


REPORT_T = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>M3U Check v17 - {date}</title>
<link rel="icon" type="image/svg+xml" href="icon.svg">
<style>{common_css}</style>
</head><body>
{theme_btn}
<a class="back" href="index.html">← на главную</a>
<h1>Отчёт проверки</h1>
<div class="sub">{date} · источников: {n_playlists}</div>
{health_block}
<div class="cards">
<div class="card"><div class="k">Проверено</div><div class="v" data-count="{total}">0</div>{sp_total}</div>
<div class="card"><div class="k">Рабочих</div><div class="v good" data-count="{ok}">0</div>{sp_ok}</div>
<div class="card"><div class="k">Нестабильных</div><div class="v warn" data-count="{unstable}">0</div></div>
<div class="card"><div class="k">Отфильтровано</div><div class="v warn" data-count="{filtered}">0</div></div>
<div class="card"><div class="k">Мёртвых</div><div class="v bad" data-count="{dead}">0</div></div>
<div class="card"><div class="k">В merged</div><div class="v" data-count="{merged}">0</div>{sp_merged}</div>
</div>
{donut_block}
{history_block}
{unstable_block}
{logo_block}
{ua_block}
{slow_block}
<div class="section"><h2>По источникам</h2>
<table><thead><tr><th>Источник</th><th>Всего</th><th>Рабочих</th><th>Нестаб.</th><th>Фильтр</th><th>%</th><th>Ср. отклик</th></tr></thead>
<tbody>{playlists_rows}</tbody></table></div>
<div class="section"><h2>Фильтры</h2>
<table><thead><tr><th>Причина</th><th>Каналов</th></tr></thead>
<tbody>{filter_rows}</tbody></table></div>
<div class="box"><a href="rejected.html">🚫 Посмотреть отсеянные каналы</a> ·
<a href="rejected.csv">📥 Скачать CSV отсеянных</a></div>
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
.qr-wrap{display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.qr-wrap img{background:#fff;padding:10px;border-radius:12px;width:200px;height:200px}
.qr-info{flex:1;min-width:200px;display:flex;flex-direction:column;gap:8px}
.link-line{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
</style></head><body>
{theme_btn}
<h1>IPTV — авто-обновляемый плейлист</h1>
<div class="sub">Обновлено: {date}</div>
<div id="wf-status" class="wf-status">Проверяю статус последнего запуска...</div>
<h2>Ссылка для плеера (M3U)</h2>
<div class="box qr-wrap">
{qr_block}
<div class="qr-info">
<div class="link-line">
<code id="player-url">{playlist_url}</code>
<button class="copy-btn" data-target="player-url">📋 Копировать</button>
</div>
<div class="sub" style="margin:0">Вставь в TiviMate / Televizo / OTT Navigator как Playlist URL</div>
</div>
</div>
<h2>Разделы</h2>
<div class="box"><a href="report.html">📊 Отчёт проверки</a></div>
<div class="box"><a href="channels.html">🔍 Поиск по каналам</a></div>
<div class="box"><a href="rejected.html">🚫 Отсеянные каналы</a></div>
<div class="box"><a href="channels.csv">📥 Скачать CSV (все каналы)</a></div>
<h2>Статистика</h2>
<div class="box">
Каналов в merged: <b>{merged_count}</b><br>
Рабочих: <b>{ok}</b> из <b>{total}</b><br>
Нестабильных отсеяно: <b>{unstable}</b>
</div>
{splits_block}
<script>
(function(){
  document.querySelectorAll('.copy-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = btn.dataset.target;
      const el = document.getElementById(id);
      if(!el) return;
      navigator.clipboard.writeText(el.textContent).then(() => {
        btn.classList.add('copied');
        const old = btn.textContent;
        btn.textContent = '✓ Скопировано';
        setTimeout(() => { btn.classList.remove('copied'); btn.textContent = old; }, 1500);
      });
    });
  });
  const gh = "{github_repo}";
  if(gh){
    fetch('https://api.github.com/repos/' + gh + '/actions/workflows/check.yml/runs?per_page=1')
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if(!d || !d.workflow_runs || !d.workflow_runs.length) return;
        const run = d.workflow_runs[0];
        const el = document.getElementById('wf-status');
        const when = new Date(run.updated_at || run.created_at);
        const ago = Math.round((Date.now() - when.getTime())/60000);
        const agoStr = ago < 1 ? 'только что' : ago < 60 ? ago + ' мин назад' : Math.round(ago/60) + ' ч назад';
        let html = '';
        if(run.status === 'in_progress') html = '<span style="color:#facc15">🔄 Идёт проверка…</span>';
        else if(run.conclusion === 'success') html = '<span style="color:#4ade80">✅ Последняя проверка успешна</span>';
        else if(run.conclusion === 'failure') html = '<span style="color:#f87171">❌ Последняя проверка упала</span>';
        else html = '<span style="color:var(--muted)">⏸ Статус: ' + (run.status||'?') + '</span>';
        html += ' <span style="color:var(--muted);font-size:12px">· ' + agoStr + '</span>';
        el.innerHTML = html;
      }).catch(() => {});
  }
})();
</script>
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
input,select{background:var(--panel);border:1px solid var(--border);color:var(--text);
padding:10px 12px;border-radius:10px;font-size:14px;width:100%;
margin-bottom:10px;font-family:inherit;backdrop-filter:blur(10px);
-webkit-backdrop-filter:blur(10px)}
.controls{display:grid;grid-template-columns:1fr 220px;gap:10px;margin-bottom:16px}
@media(max-width:600px){.controls{grid-template-columns:1fr}}
th{position:sticky;top:0;background:var(--bg);backdrop-filter:blur(20px);
-webkit-backdrop-filter:blur(20px);z-index:2}
tr:hover td{background:var(--panel)}
.logo{width:32px;height:32px;object-fit:contain;vertical-align:middle;
background:var(--border);border-radius:6px;padding:3px}
.name{font-weight:500}
.group{color:var(--muted);font-size:12px}
.copy{background:var(--border);border:none;color:var(--muted);padding:5px 10px;
border-radius:6px;font-size:11px;cursor:pointer;transition:background .15s}
.copy:hover{background:var(--accent);color:#fff}
.copy.ok{background:#4ade80;color:#0f1115}
.epg-yes{color:#4ade80;font-size:11px;margin-left:4px}
.epg-no{color:var(--muted);font-size:11px;margin-left:4px}
</style></head><body>
{theme_btn}
<a href="index.html">← на главную</a>
<h1>Каналы</h1>
<div class="sub">Всего: <span id="cnt">{total}</span> · с EPG: <span id="epg">{epg}</span> · показано: <span id="shown">{total}</span></div>
<div class="controls">
<input id="q" type="search" placeholder="Поиск по названию...">
<select id="g"><option value="">Все группы</option>{group_options}</select>
</div>
<table>
<thead><tr><th></th><th>Название</th><th>Аптайм</th><th>Группа</th><th></th></tr></thead>
<tbody id="tb"></tbody>
</table>
<script>
const CH = {channels_json};
const tb = document.getElementById('tb');
const q = document.getElementById('q');
const g = document.getElementById('g');
const shown = document.getElementById('shown');
const params = new URLSearchParams(location.search);
const initGroup = params.get('group');
if(initGroup) g.value = initGroup;
function esc(s){ return (s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function uptimeHtml(pct, samples) {
  if (pct === null || pct === undefined || samples < 2) return '<span class="uptime-none">—</span>';
  let cls = 'uptime-good';
  if (pct < 50) cls = 'uptime-low';
  else if (pct < 80) cls = 'uptime-mid';
  return '<span class="' + cls + '">' + pct + '%</span>';
}
function render(){
  const term = q.value.trim().toLowerCase();
  const grp = g.value;
  let out = [];
  for (const c of CH) {
    if (grp && c.group !== grp) continue;
    if (term && !c.name.toLowerCase().includes(term)) continue;
    out.push(c);
  }
  if (out.length > 500) out = out.slice(0, 500);
  const html = out.map(c => {
    const logo = c.logo ? '<img class="logo" src="' + esc(c.logo) + '" loading="lazy" onerror="this.style.display=\\'none\\'">' : '';
    const epg = c.tvg_id ? '<span class="epg-yes" title="' + esc(c.tvg_id) + '">EPG</span>' : '<span class="epg-no">no-EPG</span>';
    const up = uptimeHtml(c.uptime_pct, c.uptime_samples);
    return '<tr><td>' + logo + '</td>'
      + '<td class="name">' + esc(c.name) + epg + '</td>'
      + '<td>' + up + '</td>'
      + '<td class="group">' + esc(c.group) + '</td>'
      + '<td><button class="copy" data-u="' + esc(c.url) + '">URL</button></td></tr>';
  }).join('');
  tb.innerHTML = html || '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:24px">Ничего не найдено</td></tr>';
  shown.textContent = out.length;
  tb.querySelectorAll('.copy').forEach(b => b.addEventListener('click', () => {
    navigator.clipboard.writeText(b.dataset.u).then(() => {
      b.classList.add('ok');
      const t = b.textContent; b.textContent = '✓';
      setTimeout(() => { b.classList.remove('ok'); b.textContent = t; }, 1000);
    });
  }));
}
q.addEventListener('input', render);
g.addEventListener('change', render);
render();
</script>
{theme_js}
</body></html>
"""

REJECTED_T = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Отсеянные каналы</title>
<link rel="icon" type="image/svg+xml" href="icon.svg">
<style>{common_css}
input,select{background:var(--panel);border:1px solid var(--border);color:var(--text);
padding:10px 12px;border-radius:10px;font-size:14px;width:100%;
margin-bottom:10px;font-family:inherit;backdrop-filter:blur(10px);
-webkit-backdrop-filter:blur(10px)}
.controls{display:grid;grid-template-columns:1fr 260px;gap:10px;margin-bottom:16px}
@media(max-width:600px){.controls{grid-template-columns:1fr}}
th{position:sticky;top:0;background:var(--bg);backdrop-filter:blur(20px);
-webkit-backdrop-filter:blur(20px);z-index:2}
tr:hover td{background:var(--panel)}
.name{font-weight:500}
.group{color:var(--muted);font-size:12px}
.url-cell{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--muted);
max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
</style></head><body>
{theme_btn}
<a href="index.html">← на главную</a>
<h1>Отсеянные каналы</h1>
<div class="sub">Всего: <span id="cnt">{total}</span> · показано: <span id="shown">{total}</span></div>
<div class="controls">
<input id="q" type="search" placeholder="Поиск по названию или URL...">
<select id="r"><option value="">Все причины</option>{reason_options}</select>
</div>
<table>
<thead><tr><th>Название</th><th>Группа</th><th>URL</th><th>Причина</th></tr></thead>
<tbody id="tb"></tbody>
</table>
<script>
const REJ = {rejected_json};
const tb = document.getElementById('tb');
const q = document.getElementById('q');
const r = document.getElementById('r');
const shown = document.getElementById('shown');
function esc(s){ return (s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function reasonClass(rs) {
  if (!rs) return 'reason-badge';
  if (rs.startsWith('dead')) return 'reason-badge dead';
  if (rs.startsWith('unstable')) return 'reason-badge unstable';
  return 'reason-badge filter';
}
function render(){
  const term = q.value.trim().toLowerCase();
  const rs = r.value;
  let out = [];
  for (const c of REJ) {
    if (rs && !c.reason.startsWith(rs)) continue;
    if (term && !(c.name.toLowerCase().includes(term) || c.url.toLowerCase().includes(term))) continue;
    out.push(c);
  }
  if (out.length > 800) out = out.slice(0, 800);
  const html = out.map(c => {
    return '<tr>'
      + '<td class="name">' + esc(c.name) + '</td>'
      + '<td class="group">' + esc(c.group) + '</td>'
      + '<td class="url-cell" title="' + esc(c.url) + '">' + esc(c.url) + '</td>'
      + '<td><span class="' + reasonClass(c.reason) + '">' + esc(c.reason) + '</span></td>'
      + '</tr>';
  }).join('');
  tb.innerHTML = html || '<tr><td colspan="4" style="text-align:center;color:var(--muted);padding:24px">Ничего не найдено</td></tr>';
  shown.textContent = out.length;
}
q.addEventListener('input', render);
r.addEventListener('change', render);
render();
</script>
{theme_js}
</body></html>
"""

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
<defs>
<linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#4ade80"/>
<stop offset="1" stop-color="#22d3ee"/>
</linearGradient>
<radialGradient id="bg" cx="0.3" cy="0.1" r="1">
<stop offset="0" stop-color="#1a2332"/>
<stop offset="1" stop-color="#0f1115"/>
</radialGradient>
</defs>
<rect width="192" height="192" rx="44" fill="url(#bg)"/>
<rect x="28" y="50" width="136" height="92" rx="14" fill="none" stroke="url(#g)" stroke-width="6"/>
<path d="M82 78 L82 114 L116 96 Z" fill="url(#g)"/>
<circle cx="60" cy="164" r="4" fill="#8a8f98"/>
<circle cx="96" cy="164" r="4" fill="#8a8f98"/>
<circle cx="132" cy="164" r="4" fill="#8a8f98"/>
</svg>"""

MANIFEST_T = """{
  "name": "IPTV Auto",
  "short_name": "IPTV",
  "start_url": "index.html",
  "display": "standalone",
  "background_color": "#0f1115",
  "theme_color": "#0f1115",
  "icons": [
    {"src": "icon.svg", "sizes": "192x192", "type": "image/svg+xml", "purpose": "any"},
    {"src": "icon.svg", "sizes": "512x512", "type": "image/svg+xml", "purpose": "any"}
  ]
}
"""


def _sparkline_for_history(history, key):
    vals = [h.get(key, 0) for h in history][-20:]
    if len(vals) < 2:
        return ''
    return sparkline_svg(vals)


def render_report(path, stats, merged, total, ok, filt, dead, unstable,
                  dur, ua, history, groups_all, logo_stats, unstable_channels):
    rows_pl = []
    for st in stats:
        pct = (st['ok'] / st['total'] * 100) if st['total'] else 0
        avg = st.get('avg_elapsed')
        avg_s = f"{avg}s" if avg is not None else "—"
        rows_pl.append(
            f"<tr><td>{html.escape(st['label'])}</td><td>{st['total']}</td>"
            f"<td>{st['ok']}</td><td>{st.get('unstable', 0)}</td>"
            f"<td>{st.get('filtered', 0)}</td>"
            f"<td>{pct:.1f}%</td><td>{avg_s}</td></tr>")
    all_f = {}
    for st in stats:
        for r, c in st.get('filter_reasons', {}).items():
            all_f[r] = all_f.get(r, 0) + c
    f_rows = "\n".join(f"<tr><td>{html.escape(r)}</td><td>{c}</td></tr>"
                       for r, c in sorted(all_f.items(), key=lambda x: -x[1])) \
             or "<tr><td colspan='2'>нет</td></tr>"

    ua_block = ''
    if ua:
        tags = " ".join(f"<span class='tag'>{UA_ICONS.get(k,'•')} {html.escape(k)}: {v}</span>"
                        for k, v in sorted(ua.items(), key=lambda x: -x[1]))
        ua_block = f"<div class='section'><h2>По User-Agent</h2><div>{tags}</div></div>"

    unstable_block = ''
    if unstable_channels:
        unstable_channels.sort(key=lambda x: x[2])
        rows = "\n".join(
            f"<tr><td>{html.escape(nm)}</td><td>{pct}%</td><td>{tot}</td></tr>"
            for nm, u, pct, tot in unstable_channels[:30])
        unstable_block = (
            f"<div class='section'><h2>Нестабильные каналы (аптайм < {CFG.min_uptime}%)</h2>"
            f"<table><thead><tr><th>Канал</th><th>Аптайм</th><th>Проверок</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>")

    logo_block = ''
    if logo_stats:
        total_logo = sum(logo_stats.values()) or 1
        labels = {
            'from_src': 'Из источника',
            'from_id': 'По iptv-org id',
            'from_name': 'По имени из базы',
            'from_translit': 'По транслиту',
            'from_epg_icon': 'Из EPG iptvx (icon)',
            'from_prefix': 'По префиксу имени',
            'from_txt': 'Из .txt',
            'none': 'Не найдено',
        }
        rows = "\n".join(
            f"<tr><td>{labels.get(k, k)}</td><td>{v}</td>"
            f"<td>{v/total_logo*100:.1f}%</td></tr>"
            for k, v in sorted(logo_stats.items(), key=lambda x: -x[1]))
        logo_block = (
            f"<div class='section'><h2>Откуда логотипы</h2>"
            f"<table><thead><tr><th>Источник</th><th>Каналов</th><th>%</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>")

    all_slow = []
    for st in stats:
        for nm, u, el in st.get('slow', [])[:10]:
            all_slow.append((nm, u, el))
    all_slow.sort(key=lambda x: -x[2])
    all_slow = all_slow[:20]
    slow_block = ''
    if all_slow:
        rows = "\n".join(f"<tr><td>{html.escape(nm)}</td><td>{el}s</td></tr>"
                         for nm, u, el in all_slow)
        slow_block = (
            f"<div class='section'><h2>Медленные каналы (>{SLOW_THRESHOLD}s)</h2>"
            f"<table><thead><tr><th>Канал</th><th>Отклик</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>")

    hist_block = render_history_svg(history)
    donut_block = render_donut_svg(groups_all)
    denom = (ok + dead + unstable) or 1
    hpct = (ok / denom) * 100
    hb = health_bar(hpct)
    sp_total = _sparkline_for_history(history, 'total')
    sp_ok = _sparkline_for_history(history, 'ok')
    sp_merged = _sparkline_for_history(history, 'merged')
    d = int(dur)
    out = fmt(
        REPORT_T,
        common_css=COMMON_CSS, theme_btn=THEME_BTN, theme_js=THEME_JS,
        date=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        n_playlists=len(stats), total=total, ok=ok, filtered=filt, dead=dead,
        unstable=unstable, merged=merged, duration=f"{d // 60}м {d % 60}с",
        health_block=hb, history_block=hist_block, donut_block=donut_block,
        unstable_block=unstable_block, logo_block=logo_block,
        ua_block=ua_block, slow_block=slow_block,
        sp_total=sp_total, sp_ok=sp_ok, sp_merged=sp_merged,
        playlists_rows="\n".join(rows_pl), filter_rows=f_rows)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(out)


def _github_repo_from_pages(pages_url):
    if not pages_url:
        return ''
    m = re.match(r'https?://([^.]+)\.github\.io/([^/]+)', pages_url)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return ''


def render_index(path, url, merged, ok, total, unstable, splits, qr_path, pages_url):
    split_html = ''
    if splits:
        items = "\n".join(
            f"<div class='box'><a href='{fname}'>{fname}</a> — {cnt} каналов</div>"
            for fname, cnt in splits)
        split_html = f"<h2>Отдельные плейлисты</h2>{items}"
    qr_block = f'<img src="{os.path.basename(qr_path)}" alt="QR">' if os.path.isfile(qr_path) else ''
    gh_repo = _github_repo_from_pages(pages_url)
    out = fmt(
        INDEX_T,
        common_css=COMMON_CSS, theme_btn=THEME_BTN, theme_js=THEME_JS,
        date=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        playlist_url=url, merged_count=merged, ok=ok, total=total,
        unstable=unstable, qr_block=qr_block, splits_block=split_html,
        github_repo=gh_repo)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(out)


def render_channels(path, channels):
    groups = {}
    ch_list = []
    epg_count = 0
    for extinf, url in channels:
        name = get_name(extinf)
        group = get_group(extinf)
        tid = get_tvg_id(extinf)
        if tid:
            epg_count += 1
        pct, total = get_uptime_pct(url)
        m = re.search(r'tvg-logo="([^"]*)"', extinf)
        logo = m.group(1) if m else ''
        groups[group] = groups.get(group, 0) + 1
        ch_list.append({'name': name, 'group': group, 'logo': logo,
                        'url': url, 'tvg_id': tid,
                        'uptime_pct': pct, 'uptime_samples': total})
    group_opts = "\n".join(
        f'<option value="{html.escape(g)}">{html.escape(g)} ({c})</option>'
        for g, c in sorted(groups.items()))
    out = fmt(
        CHANNELS_T,
        common_css=COMMON_CSS, theme_btn=THEME_BTN, theme_js=THEME_JS,
        total=len(ch_list), epg=epg_count, group_options=group_opts,
        channels_json=json.dumps(ch_list, ensure_ascii=False))
    with open(path, 'w', encoding='utf-8') as f:
        f.write(out)


def render_rejected(path):
    seen = set()
    rows = []
    for name, group, url, reason in REJECTED:
        key = (url, reason)
        if key in seen:
            continue
        seen.add(key)
        rows.append({'name': name, 'group': group, 'url': url, 'reason': reason})
    rows.sort(key=lambda x: (x['reason'], x['group'].lower(), x['name'].lower()))

    reason_counts = {}
    for r in rows:
        base = r['reason'].split(':')[0]
        reason_counts[base] = reason_counts.get(base, 0) + 1
    reason_opts = "\n".join(
        f'<option value="{html.escape(k)}">{html.escape(k)} ({v})</option>'
        for k, v in sorted(reason_counts.items(), key=lambda x: -x[1]))

    out = fmt(
        REJECTED_T,
        common_css=COMMON_CSS, theme_btn=THEME_BTN, theme_js=THEME_JS,
        total=len(rows), reason_options=reason_opts,
        rejected_json=json.dumps(rows, ensure_ascii=False))
    with open(path, 'w', encoding='utf-8') as f:
        f.write(out)
    return len(rows)


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


def tg_report(stats, total, ok, filt, unstable, merged, dur, index_url,
              source_results, is_weekly, logo_stats, epg_count):
    lines = ["<b>M3U Check v17</b>"]
    if is_weekly:
        lines.append("🗓 <i>Еженедельный отчёт</i>")
    lines.extend([
        f"Проверено: <b>{total}</b>",
        f"Рабочих: <b>{ok}</b>",
        f"Нестабильных отсеяно: <b>{unstable}</b>",
        f"Отфильтровано: <b>{filt}</b>",
        f"В merged: <b>{merged}</b>",
        f"С EPG iptvx: <b>{epg_count}</b>",
        f"Время: {int(dur)}с",
        "",
    ])
    for st in stats:
        pct = (st['ok'] / st['total'] * 100) if st['total'] else 0
        lines.append(f"• {html.escape(st['label'])}: {st['ok']}/{st['total']} ({pct:.0f}%)")
    if logo_stats:
        total_with_logo = sum(v for k, v in logo_stats.items() if k != 'none')
        total_all = sum(logo_stats.values()) or 1
        lines.append("")
        lines.append(f"<b>Логотипы:</b> {total_with_logo}/{total_all} "
                     f"({total_with_logo/total_all*100:.0f}%)")
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
    p.add_argument('--logo-timeout', type=float, default=5.0)
    p.add_argument('--no-ssl-verify', action='store_true')
    p.add_argument('--multi-ua', action='store_true')
    p.add_argument('--no-iptv-logos', action='store_true')
    p.add_argument('--check-all-logos', action='store_true')
    p.add_argument('--split-all', action='store_true')
    p.add_argument('--weekly-backup', action='store_true')
    p.add_argument('--min-uptime', type=int, default=50)
    p.add_argument('--min-uptime-samples', type=int, default=3)
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
    global CFG, CATEGORIES, FILTERS, UPTIME_NEW, REJECTED
    CFG = parse_args()
    CFG.verify_ssl = not CFG.no_ssl_verify
    if CFG.no_ssl_verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    setup_logging(CFG.log, CFG.quiet)

    REJECTED = []

    CATEGORIES = load_json(CFG.categories)
    FILTERS = load_json(CFG.filters)
    FILTERS['_name_block'] = compile_patterns(FILTERS.get('name_blocklist', []))
    FILTERS['_name_exclude'] = compile_patterns(FILTERS.get('name_exclude', []))
    FILTERS['_group_exclude'] = compile_patterns(FILTERS.get('group_exclude', []))
    FILTERS['_url_block'] = compile_patterns(FILTERS.get('url_blocklist', []))
    FILTERS['_suspicious'] = compile_patterns(FILTERS.get('suspicious_patterns', []))
    FILTERS['_malformed'] = compile_patterns(FILTERS.get('malformed_patterns', []))

    uptime_path = os.path.join(CFG.docs_dir, 'uptime.json')
    load_uptime(uptime_path)
    if UPTIME:
        emit(f"Аптайм: загружено {len(UPTIME)} URL из истории")

    emit("Загружаю базу iptv-org (логотипы + iptv-org id)...")
    load_iptv_org()

    emit("Загружаю EPG iptvx.one (tvg-id + icon)...")
    download_epg_map()

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
    all_current_urls = []

    try:
        for path, label, check in all_sources:
            st = process_playlist(path, label, txt_logos, cache, check=check)
            if st is None:
                continue
            all_current_urls.extend(st.get('all_urls', []))
            safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', label)
            write_playlist(os.path.join(CFG.output, f"{safe}.m3u8"), st['channels'])
            for e, u in st['channels']:
                if not CFG.no_dedup and u in seen:
                    continue
                seen.add(u)
                merged_all.append((pl, e, u))
            stats_list.append(st)
            pl += 1

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
        unstable = sum(s.get('unstable', 0) for s in stats_list)
        dead = total - ok - filt - unstable
        epg_count = sum(1 for e, _ in ordered_merged if get_tvg_id(e))

        ua_totals = {}
        for s in stats_list:
            for k, c in s['ua_stats'].items():
                if k == 'skipped':
                    continue
                ua_totals[k] = ua_totals.get(k, 0) + c

        unstable_list = []
        for u, hist in UPTIME.items():
            if len(hist) < CFG.min_uptime_samples:
                continue
            pct = round(sum(hist) / len(hist) * 100)
            if pct < CFG.min_uptime:
                for st in stats_list:
                    for e, uu in st['channels']:
                        if uu == u:
                            unstable_list.append((get_name(e), u, pct, len(hist)))
                            break

        history_path = os.path.join(CFG.docs_dir, 'history.json')
        history = load_history(history_path)
        history.append({
            'date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
            'total': total, 'ok': ok, 'filtered': filt, 'unstable': unstable,
            'dead': dead, 'merged': len(ordered_merged), 'epg': epg_count,
        })
        save_history(history_path, history)

        groups_all = {}
        for e, _ in ordered_merged:
            g = get_group(e) or '(без группы)'
            groups_all[g] = groups_all.get(g, 0) + 1

        write_csv(ordered_merged, os.path.join(CFG.docs_dir, 'channels.csv'))

        rejected_count = write_rejected_csv(os.path.join(CFG.docs_dir, 'rejected.csv'))
        rejected_html_count = render_rejected(os.path.join(CFG.docs_dir, 'rejected.html'))
        emit(f"\nrejected.csv: {rejected_count} записей (уникальных)")
        emit(f"rejected.html: {rejected_html_count} записей")

        render_report(os.path.join(CFG.docs_dir, 'report.html'),
                      stats_list, len(ordered_merged), total, ok, filt, dead, unstable,
                      time.time() - started, ua_totals, history, groups_all,
                      LOGO_STATS, unstable_list)

        pages = CFG.pages_url.rstrip('/')
        purl = f'{pages}/{CFG.merged_name}' if pages else CFG.merged_name
        iurl = f'{pages}/index.html' if pages else ''
        qr_path = os.path.join(CFG.docs_dir, 'qr.png')
        if pages and HAS_QR:
            generate_qr(purl, qr_path)
        render_index(os.path.join(CFG.docs_dir, 'index.html'),
                     purl, len(ordered_merged), ok, total, unstable,
                     splits, qr_path, pages)
        render_channels(os.path.join(CFG.docs_dir, 'channels.html'), ordered_merged)
        write_pwa_assets(CFG.docs_dir)

        save_uptime(uptime_path, all_current_urls)
        emit(f"\nАптайм: сохранено {len(UPTIME)} URL в истории")

        logo_total = sum(v for k, v in LOGO_STATS.items() if k != 'none')
        logo_all = sum(LOGO_STATS.values()) or 1
        emit(f"\nГотово за {int(time.time() - started)}с. "
             f"OK={ok}, фильтр={filt}, нестабильных={unstable}, "
             f"merged={len(ordered_merged)}, "
             f"с EPG iptvx={epg_count}, с лого={logo_total}/{logo_all} ({logo_total/logo_all*100:.0f}%)")
        if LOGO_STATS:
            for k in ['from_src', 'from_id', 'from_name', 'from_translit',
                      'from_epg_icon', 'from_prefix', 'from_txt', 'none']:
                if LOGO_STATS.get(k):
                    emit(f"    лого {k}: {LOGO_STATS[k]}")

        if CFG.tg_token and CFG.tg_chat:
            is_sunday = datetime.datetime.now().weekday() == 6
            tg_report(stats_list, total, ok, filt, unstable, len(ordered_merged),
                      time.time() - started, iurl, source_results,
                      is_weekly=(CFG.weekly_backup and is_sunday),
                      logo_stats=LOGO_STATS, epg_count=epg_count)
            if CFG.tg_send_merged and merged_path:
                tg_file(CFG.tg_token, CFG.tg_chat, merged_path,
                        caption="Merged плейлист")
            if CFG.weekly_backup and is_sunday:
                for fn in ('report.html', 'channels.csv', 'channels.html', 'rejected.csv'):
                    fp = os.path.join(CFG.docs_dir, fn)
                    if os.path.isfile(fp):
                        tg_file(CFG.tg_token, CFG.tg_chat, fp,
                                caption=f"Weekly backup: {fn}")

    finally:
        cache.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
