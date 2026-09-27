#!/usr/bin/env python3
"""
M3U Checker v22 — ffprobe + whitelist + AES-256 шифрование + красивая главная.
Сплиты с случайными именами. Всё под пароль из Telegram.
Fallbacks для курсов/новостей/CDN.
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
import random
import shutil
import sqlite3
import logging
import secrets
import argparse
import threading
import datetime
import subprocess
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

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


USER_AGENTS = [
    ('wink',     'WINK/RT_(Android_TV/11)_WinkPlayer_AppleWebKit/537.36'),
    ('vlc',      'VLC/3.0.20 LibVLC/3.0.20'),
    ('tivimate', 'TiviMate/4.7.0 (Linux;Android 11) ExoPlayerLib/2.18.1'),
    ('smarttv',  'Mozilla/5.0 (SMART-TV; Linux; Tizen 6.0) AppleWebKit/537.36'),
]
DEFAULT_UA = USER_AGENTS[0][1]
UA_LINE = f'#EXTVLCOPT:http-user-agent={DEFAULT_UA}'
UA_ICONS = {'wink': '📺', 'vlc': '🎬', 'tivimate': '📱', 'smarttv': '📡', 'cached': '⚡'}

# ===== WHITELIST =====
WHITELIST_URLS = []
WHITELIST_NAMES = []
WHITELIST_STATS = {'skipped': 0, 'kept': 0}


def compile_whitelist():
    global WHITELIST_URLS, WHITELIST_NAMES
    WHITELIST_URLS = []
    WHITELIST_NAMES = []
    for p in FILTERS.get('whitelist_url_patterns', []) or []:
        if not p:
            continue
        try:
            WHITELIST_URLS.append(re.compile(p, re.IGNORECASE))
        except re.error:
            WHITELIST_URLS.append(re.compile(re.escape(p), re.IGNORECASE))
    for p in FILTERS.get('whitelist_name_patterns', []) or []:
        if not p:
            continue
        try:
            WHITELIST_NAMES.append(re.compile(p))
        except re.error:
            WHITELIST_NAMES.append(re.compile(re.escape(p), re.IGNORECASE))


def is_whitelisted(url, name):
    if url:
        for p in WHITELIST_URLS:
            if p.search(url):
                return True
    if name:
        for p in WHITELIST_NAMES:
            if p.search(name):
                return True
    return False
# ===== /WHITELIST =====

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

QUALITY_CACHE = 'docs/quality.json'
QUALITY_CACHE_MAX = 3000
FFPROBE_CACHE = 'docs/ffprobe.json'
FFPROBE_MAP_MAX = 3000

HAS_FFPROBE = shutil.which('ffprobe') is not None

HTML_PREFIXES = (
    b'<!DOCTYPE', b'<html', b'<HTML',
    b'<head', b'<HEAD', b'<?xml', b'<error',
)
BOM = b'\xef\xbb\xbf'
WHITESPACE = b' \t\r\n'
SLOW_THRESHOLD = 2.0
HISTORY_MAX = 180
UPTIME_MAX = 30
REJECTED_LIMIT = 3000
HLS_CHUNK_SIZE = 4096

QUALITY_4K_NAME = re.compile(r'\b(4k|uhd|uhk|2160p?)\b', re.IGNORECASE)
QUALITY_HD_NAME = re.compile(r'\b(hd|fhd|1080p?|720p?|h\.?264|h265|hevc)\b', re.IGNORECASE)
QUALITY_SD_NAME = re.compile(r'\b(sd|576p?|480p?|360p?|240p?)\b', re.IGNORECASE)

DONUT_COLORS = [
    '#4ade80', '#60a5fa', '#facc15', '#f87171', '#a78bfa',
    '#fb923c', '#34d399', '#f472b6', '#22d3ee', '#fbbf24',
    '#818cf8', '#fca5a5',
]
QUALITY_COLORS = {'4K': '#a78bfa', 'HD': '#4ade80', 'SD': '#facc15', 'Unknown': '#8a8f98'}

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
QUALITY_MAP = {}
QUALITY_MAP_NEW = {}
QUALITY_LOCK = threading.Lock()
FFPROBE_MAP = {}
FFPROBE_MAP_NEW = {}
FFPROBE_LOCK = threading.Lock()
FFPROBE_SEM = threading.Semaphore(4)
FFPROBE_STATS = {'ok': 0, 'fail': 0, 'cached_ok': 0, 'cached_fail': 0, 'skipped': 0}
WEATHER_HTML = '🌡️ <span class="hide-mobile">Нальчик</span>'

# ===== Обфускация сплитов =====
SPLIT_MAP_FILE = 'docs/split_names.json'
SPLIT_FILENAME_PREFIX = 'g_'


def generate_split_filename():
    alphabet = 'abcdefghijkmnpqrstuvwxyz23456789'
    name = ''.join(secrets.choice(alphabet) for _ in range(10))
    return f'{SPLIT_FILENAME_PREFIX}{name}.m3u8'


def load_split_map():
    if not os.path.isfile(SPLIT_MAP_FILE):
        return {}
    try:
        with open(SPLIT_MAP_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_split_map(m):
    os.makedirs(os.path.dirname(SPLIT_MAP_FILE) or '.', exist_ok=True)
    with open(SPLIT_MAP_FILE, 'w', encoding='utf-8') as f:
        json.dump(m, f, ensure_ascii=False, indent=2)


def cleanup_old_splits(docs_dir):
    removed = 0
    try:
        for f in os.listdir(docs_dir):
            path = os.path.join(docs_dir, f)
            if not os.path.isfile(path):
                continue
            if f.endswith('.m3u8') and (
                f.startswith('group_') or
                f.startswith(SPLIT_FILENAME_PREFIX) or
                f in ('4k.m3u8', 'hd.m3u8', 'sd.m3u8')
            ):
                try:
                    os.remove(path)
                    removed += 1
                except Exception:
                    pass
    except Exception as e:
        log.warning("cleanup_old_splits: %s", e)
    return removed


def humanize_split_name(key):
    n = key
    for suf in ('.m3u8', '.m3u'):
        if n.endswith(suf):
            n = n[:-len(suf)]
    n = n.replace('group_', '')
    n = n.replace('_', ' ').strip()
    if not n:
        return key
    if n.lower() in ('4k', 'hd', 'sd'):
        return n.upper()
    return n[:1].upper() + n[1:]
# ===== /Обфускация сплитов =====

# ===== ШИФРОВАНИЕ =====
def generate_password(length=16):
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def encrypt_payload(payload_dict, password, iterations=150000):
    if not HAS_CRYPTO:
        raise RuntimeError("cryptography не установлен")
    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=iterations)
    key = kdf.derive(password.encode('utf-8'))
    payload = json.dumps(payload_dict, ensure_ascii=False).encode('utf-8')
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(iv, payload, None)
    return base64.b64encode(salt + iv + ciphertext).decode('ascii')


def send_password_to_telegram(password, playlist_url):
    if not CFG.tg_token or not CFG.tg_chat:
        log.warning("⚠️  Telegram не настроен — пароль не отправлен")
        return False
    pages = CFG.pages_url.rstrip('/') if CFG.pages_url else ''
    text = (
        f"🔐 <b>Новый пароль для страницы</b>\n\n"
        f"<code>{password}</code>\n\n"
        f"📍 Открывай: <a href='{pages}/index.html'>index.html</a>\n"
        f"⏱ Действует до следующего запуска (6 часов)\n"
        f"📺 Внутри — ссылки на плейлисты и QR"
    )
    return tg_send(CFG.tg_token, CFG.tg_chat, text)
# ===== /ШИФРОВАНИЕ =====


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


def _weather_emoji(code):
    try:
        code = int(code)
    except Exception:
        return '🌡️'
    if code == 0: return '☀️'
    if code <= 3: return '⛅'
    if code <= 48: return '🌫️'
    if code <= 57: return '🌦️'
    if code <= 67: return '🌧️'
    if code <= 77: return '❄️'
    if code <= 82: return '🌧️'
    if code <= 86: return '❄️'
    return '⛈️'


def fetch_weather():
    try:
        r = requests.get('https://wttr.in/Nalchik?format=j1',
                         timeout=10, verify=CFG.verify_ssl)
        if r.status_code == 200:
            d = r.json()
            cc = (d.get('current_condition') or [{}])[0]
            t = cc.get('temp_C')
            code = cc.get('weatherCode') or 0
            if t is not None:
                return f'{_weather_emoji(code)} <strong>{t}°C</strong> <span class="hide-mobile">· Нальчик</span>'
    except Exception as e:
        log.debug("wttr.in: %s", e)
    try:
        r = requests.get(
            'https://api.open-meteo.com/v1/forecast'
            '?latitude=43.4981&longitude=43.6189'
            '&current=temperature_2m,weather_code&timezone=Europe%2FMoscow',
            timeout=10, verify=CFG.verify_ssl)
        if r.status_code == 200:
            c = r.json().get('current') or {}
            t = c.get('temperature_2m')
            code = c.get('weather_code') or 0
            if t is not None:
                return f'{_weather_emoji(code)} <strong>{round(t)}°C</strong> <span class="hide-mobile">· Нальчик</span>'
    except Exception as e:
        log.debug("open-meteo: %s", e)
    return '🌡️ <span class="hide-mobile">Нальчик</span>'


def fetch_weather_v2():
    try:
        r = requests.get('https://wttr.in/Nalchik?format=j1',
                         timeout=10, verify=CFG.verify_ssl)
        if r.status_code == 200:
            d = r.json()
            cur = d['current_condition'][0]
            today = d['weather'][0]
            astro = today['astronomy'][0]
            return {
                'now_temp': cur['temp_C'],
                'now_feels': cur['FeelsLikeC'],
                'now_code': int(cur.get('weatherCode', 0)),
                'now_humidity': cur.get('humidity', '—'),
                'now_wind': cur.get('windspeedKmph', '—'),
                'now_pressure': cur.get('pressure', '—'),
                'sunrise': astro.get('sunrise', ''),
                'sunset': astro.get('sunset', ''),
                'forecast': [
                    {'date': day['date'], 'max': day['maxtempC'],
                     'min': day['mintempC'],
                     'code': int(day['hourly'][4].get('weatherCode', 0))}
                    for day in d['weather'][:3]
                ],
            }
    except Exception as e:
        log.debug("weather_v2: %s", e)
    return None


def weather_kind_v2(code):
    if code == 0: return 'sun'
    if code <= 3: return 'cloud'
    if code <= 48: return 'fog'
    if code <= 67: return 'rain'
    if code <= 77: return 'snow'
    if code <= 82: return 'rain'
    if code <= 86: return 'snow'
    return 'storm'


def fetch_currency():
    """Курсы ЦБ. Fallback: cbr-xml-daily.ru → cbr.ru → exchangerate.host"""
    # 1. cbr-xml-daily.ru
    try:
        r = requests.get('https://www.cbr-xml-daily.ru/daily_json.js',
                         timeout=10, verify=CFG.verify_ssl)
        if r.status_code == 200:
            v = r.json()['Valute']
            return {'usd': round(v['USD']['Value'], 2),
                    'eur': round(v['EUR']['Value'], 2),
                    'cny': round(v['CNY']['Value'], 2),
                    'src': 'cbr-xml'}
    except Exception as e:
        log.debug("cbr-xml-daily: %s", e)

    # 2. Официальный cbr.ru (XML)
    try:
        r = requests.get('https://www.cbr.ru/scripts/XML_daily.asp',
                         timeout=15, verify=CFG.verify_ssl,
                         headers={'User-Agent': DEFAULT_UA})
        if r.status_code == 200:
            import xml.etree.ElementTree as ET
            r.encoding = 'windows-1251'
            root = ET.fromstring(r.text)
            out = {}
            for val in root.findall('Valute'):
                code = val.findtext('CharCode')
                value = val.findtext('Value')
                nominal = val.findtext('Nominal') or '1'
                if code in ('USD', 'EUR', 'CNY') and value:
                    try:
                        v = float(value.replace(',', '.')) / int(nominal)
                        out[code.lower()] = round(v, 2)
                    except Exception:
                        pass
            if out.get('usd') and out.get('eur') and out.get('cny'):
                out['src'] = 'cbr.ru'
                return out
    except Exception as e:
        log.debug("cbr.ru: %s", e)

    # 3. exchangerate.host
    try:
        r = requests.get('https://api.exchangerate.host/latest?base=RUB&symbols=USD,EUR,CNY',
                         timeout=10, verify=CFG.verify_ssl)
        if r.status_code == 200:
            rates = r.json().get('rates', {})
            if rates.get('USD') and rates.get('EUR') and rates.get('CNY'):
                return {
                    'usd': round(1 / rates['USD'], 2),
                    'eur': round(1 / rates['EUR'], 2),
                    'cny': round(1 / rates['CNY'], 2),
                    'src': 'exchangerate.host',
                }
    except Exception as e:
        log.debug("exchangerate.host: %s", e)
    return None


def fetch_news_v2(limit=4):
    """Новости. Fallback: lenta.ru → rbc.ru → tass.ru"""
    sources = [
        ('https://lenta.ru/rss/news', 'rss'),
        ('https://rssexport.rbc.ru/rbcnews/news/30/full.rss', 'rss'),
        ('https://tass.ru/rss/v2.xml', 'rss'),
    ]
    import xml.etree.ElementTree as ET
    for url, kind in sources:
        try:
            r = requests.get(url, timeout=15, verify=CFG.verify_ssl,
                             headers={'User-Agent': DEFAULT_UA})
            if r.status_code != 200:
                continue
            r.encoding = 'utf-8'
            root = ET.fromstring(r.content)
            items = []
            for item in root.iter('item'):
                title_el = item.find('title')
                link_el = item.find('link')
                desc_el = item.find('description')
                if title_el is None or not title_el.text:
                    continue
                desc = ''
                if desc_el is not None and desc_el.text:
                    desc = re.sub(r'<[^>]+>', '', desc_el.text)
                    desc = html.unescape(desc).strip()
                    if len(desc) > 110:
                        desc = desc[:107] + '...'
                items.append({
                    'title': title_el.text.strip(),
                    'link': (link_el.text or '').strip() if link_el is not None else '',
                    'desc': desc,
                })
                if len(items) >= limit:
                    break
            if items:
                log.debug("news: источник %s", url)
                return items
        except Exception as e:
            log.debug("news %s: %s", url, e)
    return []


def moon_phase_v2():
    today = datetime.date.today()
    known = datetime.date(2000, 1, 6)
    days = (today - known).days
    phase = (days % 29.530588853) / 29.530588853
    if phase < 0.03 or phase > 0.97: return '🌑', 'Новолуние'
    if phase < 0.22: return '🌒', 'Молодая луна'
    if phase < 0.28: return '🌓', 'Первая четверть'
    if phase < 0.47: return '🌔', 'Растущая луна'
    if phase < 0.53: return '🌕', 'Полнолуние'
    if phase < 0.72: return '🌖', 'Убывающая луна'
    if phase < 0.78: return '🌗', 'Последняя четверть'
    return '🌘', 'Старая луна'


QUOTES_RU = [
    ("Жизнь — это то, что происходит, пока ты строишь планы.", "Джон Леннон"),
    ("Единственный способ делать великую работу — любить то, что делаешь.", "Стив Джобс"),
    ("Не бойся медленно идти, бойся стоять на месте.", "Китайская пословица"),
    ("Дорогу осилит идущий.", "Латинская пословица"),
    ("Всё гениальное — просто.", "Исаак Ньютон"),
    ("Кто хочет — ищет возможности, кто не хочет — ищет причины.", "Сократ"),
    ("Самый тёмный час — перед рассветом.", "Пауло Коэльо"),
    ("Меняй свою жизнь, а не мир вокруг.", "Лев Толстой"),
    ("Будь тем изменением, которое ты хочешь видеть в мире.", "Махатма Ганди"),
    ("Цель без плана — просто желание.", "Антуан де Сент-Экзюпери"),
    ("Мы то, что мы делаем постоянно. Совершенство — не действие, а привычка.", "Аристотель"),
    ("Победа над собой — величайшая из побед.", "Платон"),
]

HOLIDAYS_RU = {
    (1, 1): "🎄 Новый год", (1, 7): "🌟 Рождество Христово",
    (1, 14): "🎊 Старый Новый год",
    (2, 23): "🎖 День защитника Отечества",
    (3, 8): "💐 Международный женский день",
    (4, 12): "🚀 День космонавтики",
    (5, 1): "🌷 Праздник Весны и Труда",
    (5, 9): "🎗 День Победы",
    (6, 1): "👶 День защиты детей",
    (6, 12): "🇷🇺 День России",
    (9, 1): "📚 День знаний",
    (11, 4): "🕊 День народного единства",
}

WEEKDAYS_SHORT_RU = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
MONTHS_RU_NOM = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
                 'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']


def quote_of_day():
    today = datetime.date.today()
    idx = (today.year * 366 + today.timetuple().tm_yday) % len(QUOTES_RU)
    return QUOTES_RU[idx]


def days_to_new_year():
    today = datetime.date.today()
    return (datetime.date(today.year + 1, 1, 1) - today).days


def calendar_html(today):
    year, month = today.year, today.month
    first = datetime.date(year, month, 1)
    start_wd = first.weekday()
    nm = datetime.date(year + 1, 1, 1) if month == 12 else datetime.date(year, month + 1, 1)
    days_in = (nm - first).days
    cells = []
    for _ in range(start_wd):
        cells.append('<div class="cal-cell cal-empty"></div>')
    for d in range(1, days_in + 1):
        date = datetime.date(year, month, d)
        cls = 'cal-cell'
        if d == today.day:
            cls += ' cal-today'
        elif date.weekday() >= 5:
            cls += ' cal-weekend'
        hol = HOLIDAYS_RU.get((month, d))
        hol_attr = f' title="{hol}"' if hol else ''
        hol_dot = '<span class="cal-hol">•</span>' if hol else ''
        cells.append(f'<div class="{cls}"{hol_attr}>{d}{hol_dot}</div>')
    headers = ''.join(f'<div class="cal-head">{w}</div>' for w in WEEKDAYS_SHORT_RU)
    return (f'<div class="cal-month">{MONTHS_RU_NOM[month-1]} {year}</div>'
            f'<div class="cal-grid">{headers}{"".join(cells)}</div>')


def generate_svg_art():
    seed = random.randint(0, 999999)
    random.seed(seed)
    palettes = [
        ('#667eea', '#764ba2', '#f093fb'),
        ('#4facfe', '#00f2fe', '#43e97b'),
        ('#fa709a', '#fee140', '#ff6b6b'),
        ('#30cfd0', '#330867', '#a8edea'),
        ('#a1c4fd', '#c2e9fb', '#fbc2eb'),
        ('#43e97b', '#38f9d7', '#fa709a'),
        ('#5ee7df', '#b490ca', '#7b68ee'),
    ]
    c1, c2, c3 = random.choice(palettes)
    blobs = ''.join(
        f'<circle cx="{random.randint(0,300)}" cy="{random.randint(0,200)}" '
        f'r="{random.randint(50,130)}" '
        f'fill="{random.choice([c1,c2,c3])}" '
        f'opacity="{random.uniform(0.3,0.7):.2f}"/>'
        for _ in range(6)
    )
    lines = ''.join(
        f'<line x1="{random.randint(0,300)}" y1="{random.randint(0,200)}" '
        f'x2="{random.randint(0,300)}" y2="{random.randint(0,200)}" '
        f'stroke="white" stroke-width="{random.uniform(0.3,0.8):.1f}" '
        f'opacity="{random.uniform(0.1,0.3):.2f}"/>'
        for _ in range(8)
    )
    particles = ''.join(
        f'<circle cx="{random.randint(0,300)}" cy="{random.randint(0,200)}" '
        f'r="{random.uniform(0.5,2):.1f}" fill="white" '
        f'opacity="{random.uniform(0.2,0.7):.2f}"/>'
        for _ in range(40)
    )
    return (
        f'<svg viewBox="0 0 300 200" xmlns="http://www.w3.org/2000/svg" '
        f'preserveAspectRatio="xMidYMid slice">'
        f'<defs>'
        f'<filter id="b{seed}" x="-50%" y="-50%" width="200%" height="200%">'
        f'<feGaussianBlur stdDeviation="22"/></filter>'
        f'<radialGradient id="v{seed}" cx="50%" cy="50%" r="70%">'
        f'<stop offset="60%" stop-color="rgba(0,0,0,0)"/>'
        f'<stop offset="100%" stop-color="rgba(0,0,0,0.5)"/></radialGradient>'
        f'</defs>'
        f'<rect width="300" height="200" fill="#0a0e1a"/>'
        f'<g filter="url(#b{seed})">{blobs}</g>'
        f'{lines}{particles}'
        f'<rect width="300" height="200" fill="url(#v{seed})"/>'
        f'</svg>'
    )


def render_weather_card():
    w = fetch_weather_v2()
    if not w:
        return '<div class="wx-empty">Погода недоступна</div>', 'cloud'
    kind = weather_kind_v2(w['now_code'])
    fc = ''.join(
        f'<div class="fc-day">'
        f'<div class="fc-date">{d["date"][5:].replace("-", ".")}</div>'
        f'<div class="fc-icon">{_weather_emoji(d["code"])}</div>'
        f'<div class="fc-temps"><span class="fc-max">↑{d["max"]}°</span>'
        f'<span class="fc-min">↓{d["min"]}°</span></div></div>'
        for d in w['forecast']
    )
    out = (
        f'<div class="wx-hero">'
        f'<div class="wx-icon">{_weather_emoji(w["now_code"])}</div>'
        f'<div><div class="wx-temp">{w["now_temp"]}°</div>'
        f'<div class="wx-feels">ощущается {w["now_feels"]}°</div></div></div>'
        f'<div class="wx-meta">'
        f'<div class="wx-meta-item"><span>💧</span><b>{w["now_humidity"]}%</b><small>влажн.</small></div>'
        f'<div class="wx-meta-item"><span>💨</span><b>{w["now_wind"]}</b><small>км/ч</small></div>'
        f'<div class="wx-meta-item"><span>🔽</span><b>{w["now_pressure"]}</b><small>мм</small></div>'
        f'</div>'
        f'<div class="wx-sun">'
        f'<div class="wx-sun-item"><span>🌅</span> Восход <b>{w["sunrise"]}</b></div>'
        f'<div class="wx-sun-item"><span>🌇</span> Закат <b>{w["sunset"]}</b></div>'
        f'</div>'
        f'<div class="wx-forecast">{fc}</div>'
    )
    return out, kind


def render_currency_card():
    c = fetch_currency()
    if not c:
        return '<div class="cur-row">—</div>'
    return (
        f'<div class="cur-row"><span class="cur-flag">🇺🇸</span>'
        f'<span class="cur-name">USD</span><span class="cur-val">{c["usd"]} ₽</span></div>'
        f'<div class="cur-row"><span class="cur-flag">🇪🇺</span>'
        f'<span class="cur-name">EUR</span><span class="cur-val">{c["eur"]} ₽</span></div>'
        f'<div class="cur-row"><span class="cur-flag">🇨🇳</span>'
        f'<span class="cur-name">CNY</span><span class="cur-val">{c["cny"]} ₽</span></div>'
    )


def render_news_card():
    news = fetch_news_v2(limit=4)
    if not news:
        return '<div class="news-item">Новости недоступны</div>'
    return ''.join(
        f'<a class="news-item" href="{html.escape(n["link"])}" target="_blank" rel="noopener">'
        f'<div class="news-title">{html.escape(n["title"])}</div>'
        f'{f"""<div class="news-desc">{html.escape(n["desc"])}</div>""" if n["desc"] else ""}'
        f'</a>'
        for n in news
    )


# ---------- Основные утилиты ----------
_TRANSLIT_MAP = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh',
    'з':'z','и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o',
    'п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'ts',
    'ч':'ch','ш':'sh','щ':'sch','ъ':'','ы':'y','ь':'','э':'e',
    'ю':'yu','я':'ya','і':'i','ї':'i','є':'e','ґ':'g',
}
NOISE_WORDS = re.compile(
    r'\b(тв|tv|телеканал|channel|канал|hd|fhd|uhd|4k|8k|sd|hevc|h265|h264|mp4|hq|lq|'
    r'ру|ru|россия|russia|online|live)\b', re.IGNORECASE)
ROMAN_TAIL = re.compile(r'\b(i{1,3}|iv|v|vi{1,3}|ix|x)\s*$', re.IGNORECASE)


def translit_ru(s):
    if not s: return ''
    return ''.join(_TRANSLIT_MAP.get(ch, ch) for ch in s.lower())


def normalize_name_v2(name):
    if not name: return ''
    n = name.lower().strip().replace('ё', 'е').replace('й', 'и')
    n = re.sub(r'\([^)]*\)', ' ', n)
    n = re.sub(r'\[[^\]]*\]', ' ', n)
    n = re.sub(r'<[^>]*>', ' ', n)
    n = re.sub(r'[^a-zа-я0-9]+', ' ', n)
    n = NOISE_WORDS.sub(' ', n)
    n = ROMAN_TAIL.sub('', n)
    return re.sub(r'\s+', ' ', n).strip()


def normalize_name(name): return normalize_name_v2(name)


def normalize_name_translit(name):
    n = normalize_name_v2(name)
    return translit_ru(n) if n else ''


def make_prefix(normalized):
    if not normalized: return ''
    s = normalized.replace(' ', '')
    return s[:5] if len(s) >= 5 else ''


def detect_quality_by_name(name):
    if not name: return 'Unknown'
    if QUALITY_4K_NAME.search(name): return '4K'
    if QUALITY_HD_NAME.search(name): return 'HD'
    if QUALITY_SD_NAME.search(name): return 'SD'
    return 'Unknown'


def classify_height(h):
    try:
        h = int(h)
    except Exception:
        return None
    if h >= 2000: return '4K'
    if h >= 700: return 'HD'
    if h > 0: return 'SD'
    return None


def parse_hls_quality(chunk):
    try:
        if not chunk or b'#EXTM3U' not in chunk[:32]:
            return None
        matches = re.findall(rb'RESOLUTION=(\d+)x(\d+)', chunk)
        if matches:
            heights = []
            for w, h in matches:
                try:
                    heights.append(int(h))
                except Exception:
                    continue
            if heights:
                return classify_height(max(heights))
        bw_matches = re.findall(rb'BANDWIDTH=(\d+)', chunk)
        if bw_matches:
            try:
                bw = max(int(x) for x in bw_matches)
            except Exception:
                return None
            if bw >= 8_000_000: return '4K'
            if bw >= 1_500_000: return 'HD'
            if bw > 0: return 'SD'
        return None
    except Exception:
        return None


def load_uptime(path):
    global UPTIME
    if not os.path.isfile(path):
        UPTIME = {}
        return
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        UPTIME = {k: v for k, v in data.items() if isinstance(v, list)} if isinstance(data, dict) else {}
    except Exception:
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


def load_quality_cache(path):
    global QUALITY_MAP
    if not os.path.isfile(path):
        QUALITY_MAP = {}
        return
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        QUALITY_MAP = data if isinstance(data, dict) else {}
    except Exception:
        QUALITY_MAP = {}


def save_quality_cache(path, current_urls):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with QUALITY_LOCK:
        for url, q in QUALITY_MAP_NEW.items():
            if q:
                QUALITY_MAP[url] = q
        alive = set(current_urls)
        for url in list(QUALITY_MAP.keys()):
            if url not in alive:
                del QUALITY_MAP[url]
        if len(QUALITY_MAP) > QUALITY_CACHE_MAX:
            items = list(QUALITY_MAP.items())[-QUALITY_CACHE_MAX:]
            QUALITY_MAP.clear()
            QUALITY_MAP.update(dict(items))
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(QUALITY_MAP, f, ensure_ascii=False, separators=(',', ':'))
    except Exception:
        pass


def get_quality_for_url(url, name):
    with QUALITY_LOCK:
        q = QUALITY_MAP.get(url)
    if q and q != 'Unknown':
        return q
    return detect_quality_by_name(name)


def load_ffprobe_cache(path):
    global FFPROBE_MAP
    if not os.path.isfile(path):
        FFPROBE_MAP = {}
        return
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        FFPROBE_MAP = data if isinstance(data, dict) else {}
    except Exception:
        FFPROBE_MAP = {}


def save_ffprobe_cache(path, current_urls):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with FFPROBE_LOCK:
        for url, entry in FFPROBE_MAP_NEW.items():
            FFPROBE_MAP[url] = entry
        alive = set(current_urls)
        for url in list(FFPROBE_MAP.keys()):
            if url not in alive:
                del FFPROBE_MAP[url]
        if len(FFPROBE_MAP) > FFPROBE_MAP_MAX:
            items = list(FFPROBE_MAP.items())[-FFPROBE_MAP_MAX:]
            FFPROBE_MAP.clear()
            FFPROBE_MAP.update(dict(items))
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(FFPROBE_MAP, f, ensure_ascii=False, separators=(',', ':'))
    except Exception:
        pass


def ffprobe_check(url):
    if not HAS_FFPROBE:
        FFPROBE_STATS['skipped'] += 1
        return None, None
    with FFPROBE_LOCK:
        cached = FFPROBE_MAP.get(url)
    if cached:
        age_days = (time.time() - cached.get('ts', 0)) / 86400
        if age_days < CFG.ffprobe_cache_days:
            if cached.get('ok'):
                FFPROBE_STATS['cached_ok'] += 1
                return True, cached
            else:
                FFPROBE_STATS['cached_fail'] += 1
                return False, None
    with FFPROBE_SEM:
        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'quiet', '-print_format', 'json',
                 '-show_streams', '-show_format',
                 '-analyzeduration', '3000000', '-probesize', '1000000',
                 '-user_agent', DEFAULT_UA, '-i', url],
                capture_output=True, timeout=CFG.ffprobe_timeout
            )
            if result.returncode != 0:
                FFPROBE_STATS['fail'] += 1
                with FFPROBE_LOCK:
                    FFPROBE_MAP_NEW[url] = {'ok': False, 'ts': time.time()}
                return False, None
            try:
                data = json.loads(result.stdout)
            except Exception:
                FFPROBE_STATS['fail'] += 1
                with FFPROBE_LOCK:
                    FFPROBE_MAP_NEW[url] = {'ok': False, 'ts': time.time()}
                return False, None
            streams = data.get('streams', [])
            video = next((s for s in streams if s.get('codec_type') == 'video'), None)
            if not video:
                FFPROBE_STATS['fail'] += 1
                with FFPROBE_LOCK:
                    FFPROBE_MAP_NEW[url] = {'ok': False, 'ts': time.time()}
                return False, None
            info = {'ok': True, 'codec': video.get('codec_name'),
                    'height': video.get('height'), 'width': video.get('width'),
                    'ts': time.time()}
            FFPROBE_STATS['ok'] += 1
            with FFPROBE_LOCK:
                FFPROBE_MAP_NEW[url] = info
            return True, info
        except subprocess.TimeoutExpired:
            FFPROBE_STATS['fail'] += 1
            with FFPROBE_LOCK:
                FFPROBE_MAP_NEW[url] = {'ok': False, 'ts': time.time()}
            return False, None
        except Exception as e:
            log.debug("ffprobe error %s: %s", url, e)
            FFPROBE_STATS['fail'] += 1
            return False, None


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
    logos = _download_json(IPTV_ORG_URLS_LOGOS, IPTV_ORG_LOGOS_CACHE) or []

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
            _add_to_prefix_map(by_prefix_logo, make_prefix(k), (k, url))

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
            _add_to_prefix_map(by_prefix_id, make_prefix(k), (k, cid))

    IPTV_IDS['by_name'] = by_name_id
    IPTV_IDS['by_name_translit'] = by_name_id_translit
    IPTV_IDS['by_prefix'] = by_prefix_id

    emit(f"  Логотипов: {len(by_id)} по id, {len(by_name_logo)} по имени")
    emit(f"  iptv-org id: {len(by_name_id)} по имени")


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
                emit(f"  EPG: из кэша v{cached_ver} {len(EPG_MAP['by_name'])} имён")
                return
        except Exception:
            pass

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
            head = xml_data[:200].decode('ascii', errors='ignore')
            if 'windows-1251' in head or 'cp1251' in head:
                xml_str = xml_data.decode('windows-1251', errors='replace')
            else:
                xml_str = xml_data.decode('utf-8', errors='replace')

            ch_re = re.compile(r'<channel\s+id="([^"]+)"[^>]*>(.*?)</channel>', re.DOTALL)
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
                    _add_to_prefix_map(by_prefix, make_prefix(k), (k, cid))

            EPG_MAP['by_name'] = by_name
            EPG_MAP['by_translit'] = by_translit
            EPG_MAP['by_prefix'] = by_prefix
            EPG_MAP['icons'] = icons
            emit(f"  EPG: {len(by_name)} имён, {len(icons)} icon")

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
    emit("  EPG: не удалось скачать")


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


def _try_stream(url, ua):
    try:
        t0 = time.time()
        with requests.get(url, headers={'User-Agent': ua}, stream=True,
                          timeout=CFG.timeout, allow_redirects=True,
                          verify=CFG.verify_ssl) as r:
            elapsed = time.time() - t0
            if r.status_code != 200:
                return False, elapsed, f'http_{r.status_code}', None
            chunk = next(r.iter_content(chunk_size=HLS_CHUNK_SIZE), b'')
            if not chunk:
                return False, elapsed, 'empty', None
            h = chunk.lstrip(WHITESPACE)
            if h.startswith(BOM):
                h = h[len(BOM):].lstrip(WHITESPACE)
            if h.startswith(b'#EXTM3U'):
                quality = None if CFG.no_hls_quality else parse_hls_quality(chunk)
                return True, elapsed, None, quality
            for p in HTML_PREFIXES:
                if h.startswith(p):
                    return False, elapsed, 'html', None
            if len(chunk) < 32:
                return False, elapsed, 'too_small', None
            return True, elapsed, None, None
    except requests.RequestException:
        return False, None, 'timeout', None


def _check_stream(url):
    uas = USER_AGENTS if CFG.multi_ua else USER_AGENTS[:1]
    best_elapsed = None
    last_reason = 'unknown'
    for name, ua in uas:
        ok, el, reason, quality = _try_stream(url, ua)
        if ok:
            return True, name, el, None, quality
        if reason:
            last_reason = reason
        if el is not None and (best_elapsed is None or el < best_elapsed):
            best_elapsed = el
    return False, None, best_elapsed, last_reason, None


def check_stream(url, cache):
    c = cache.get(url, 'stream')
    if c is not None:
        with QUALITY_LOCK:
            q = QUALITY_MAP.get(url)
        return c, 'cached', None, None, q
    ok, n, el, reason, quality = _check_stream(url)
    cache.put(url, 'stream', ok)
    if ok and quality:
        with QUALITY_LOCK:
            QUALITY_MAP_NEW[url] = quality
    return ok, (n or DEFAULT_UA), el, reason, quality


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
    if not prefix:
        return None
    items = prefix_map.get(prefix)
    if not items:
        return None
    if len(items) > 3:
        return None
    return sorted(items, key=lambda x: x[0])[0][1]


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
        if k:
            l = _pick_prefix_match(IPTV_LOGOS.get('by_prefix', {}), make_prefix(k))
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
        cid = _pick_prefix_match(IPTV_IDS.get('by_prefix', {}), make_prefix(k))
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
        cid = _pick_prefix_match(EPG_MAP.get('by_prefix', {}), make_prefix(k))
        if cid:
            return cid
    return None


def process_channel(index, extinf, url, txt_logos, cache, source_name):
    name = get_name(extinf)
    group = get_group(extinf)
    is_wl = is_whitelisted(url, name)

    if is_wl:
        WHITELIST_STATS['skipped'] += 1
        ua = 'wink'
        elapsed = None
        hls_quality = None
    else:
        filtered, reason = is_filtered(name, group, url)
        if filtered:
            with REJECTED_LOCK:
                if len(REJECTED) < REJECTED_LIMIT:
                    REJECTED.append((name, group, url, reason))
            return index, 'filtered', None, None, reason, None, None, None, None, None
        ok, ua, elapsed, fail_reason, hls_quality = check_stream(url, cache)
        if not ok:
            with UPTIME_LOCK:
                UPTIME_NEW[url] = 0
            with REJECTED_LOCK:
                if len(REJECTED) < REJECTED_LIMIT:
                    REJECTED.append((name, group, url, f'dead:{fail_reason or "unknown"}'))
            return index, 'dead', None, None, None, None, None, None, None, None
        with UPTIME_LOCK:
            UPTIME_NEW[url] = 1
        if is_migayushchiy(url):
            pct, total = get_uptime_pct(url)
            with REJECTED_LOCK:
                if len(REJECTED) < REJECTED_LIMIT:
                    REJECTED.append((name, group, url, f'unstable:{pct}%'))
            return index, 'unstable', None, None, f"uptime {pct}%", None, None, None, None, None
        if CFG.ffprobe:
            ff_ok, ff_info = ffprobe_check(url)
            if ff_ok is False:
                with REJECTED_LOCK:
                    if len(REJECTED) < REJECTED_LIMIT:
                        REJECTED.append((name, group, url, 'dead:ffprobe'))
                return index, 'dead', None, None, None, None, None, None, None, None
            if ff_ok is True and ff_info:
                h = ff_info.get('height')
                if h:
                    fq = classify_height(h)
                    if fq:
                        hls_quality = fq

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
    final_quality = hls_quality or detect_quality_by_name(name)
    if is_wl:
        WHITELIST_STATS['kept'] += 1
    return index, 'ok', extinf, url, None, ua, new_group, elapsed, logo_src, final_quality


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
                'logo_stats': {}, 'all_urls': [], 'quality_stats': {}}
    emit(f"    Каналов: {len(channels)}")
    if not check:
        out, all_urls = [], []
        for i, (extinf, url) in enumerate(channels):
            all_urls.append(url)
            name = get_name(extinf)
            group = get_group(extinf)
            is_wl = is_whitelisted(url, name)
            if not is_wl:
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
                'logo_stats': {}, 'all_urls': all_urls, 'quality_stats': {}}
    pbar = None
    if HAS_TQDM and not CFG.quiet and not CFG.no_progress:
        pbar = tqdm(total=len(channels), desc=label, unit='ch', ncols=90, leave=True)
    valid, ok_cnt, err_cnt, filt_cnt, unstable_cnt = [], 0, 0, 0, 0
    ua_stats, filt_by_reason = {}, {}
    slow_list, elapsed_list = [], []
    logo_counts = {}
    all_urls = []
    quality_stats = {'4K': 0, 'HD': 0, 'SD': 0, 'Unknown': 0}
    with concurrent.futures.ThreadPoolExecutor(max_workers=CFG.workers) as ex:
        futs = [ex.submit(process_channel, i, e, u, txt_logos, cache, label)
                for i, (e, u) in enumerate(channels)]
        for fu in concurrent.futures.as_completed(futs):
            try:
                idx, status, extinf, url, msg, ua, grp, el, logo_src, quality = fu.result()
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
                if quality:
                    quality_stats[quality] = quality_stats.get(quality, 0) + 1
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
            'logo_stats': logo_counts, 'all_urls': all_urls,
            'quality_stats': quality_stats}


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
    for key, groups in splits.items():
        wanted = set(groups)
        sel = []
        for extinf, url in channels:
            g = get_group(extinf)
            base = re.sub(r'^\S+\s+', '', g) if g else ''
            if g in wanted or base in wanted:
                sel.append((extinf, url))
        if sel:
            random_name = generate_split_filename()
            write_playlist(os.path.join(docs_dir, random_name), sel)
            created.append({
                'key': key,
                'name': humanize_split_name(key),
                'count': len(sel),
                'file': random_name,
                'type': 'group',
            })
    return created


def write_quality_splits(channels, docs_dir):
    by_q = {'4K': [], 'HD': [], 'SD': []}
    for extinf, url in channels:
        q = get_quality_for_url(url, get_name(extinf))
        if q in by_q:
            by_q[q].append((extinf, url))
    created = []
    for q, chans in by_q.items():
        if chans:
            random_name = generate_split_filename()
            write_playlist(os.path.join(docs_dir, random_name), chans)
            created.append({
                'key': f'{q.lower()}.m3u8',
                'name': q,
                'count': len(chans),
                'file': random_name,
                'type': 'quality',
            })
    return created


def write_csv(channels, path):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['name', 'group', 'quality', 'tvg_id', 'uptime_pct',
                    'uptime_samples', 'logo', 'url'])
        for extinf, url in channels:
            name = get_name(extinf)
            group = get_group(extinf)
            tid = get_tvg_id(extinf)
            pct, total = get_uptime_pct(url)
            pct_v = pct if pct is not None else ''
            m = re.search(r'tvg-logo="([^"]*)"', extinf)
            logo = m.group(1) if m else ''
            q = get_quality_for_url(url, name)
            w.writerow([name, group, q, tid, pct_v, total, logo, url])


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


def render_donut_svg(groups_dict, colors_list=None, title='Распределение по группам',
                     unit='КАНАЛОВ'):
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
        if isinstance(colors_list, dict):
            color = colors_list.get(name, DONUT_COLORS[i % len(DONUT_COLORS)])
        else:
            color = DONUT_COLORS[i % len(DONUT_COLORS)]
        svg_parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{R}" fill="none" '
            f'stroke="{color}" stroke-width="{SW}" '
            f'stroke-dasharray="{dash:.2f} {C - dash:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 {cx} {cy})"/>')
        offset += dash
        legend.append(
            f'<div class="legend-row">'
            f'<span class="legend-dot" style="background:{color}"></span>'
            f'<span class="legend-name">{html.escape(name)}</span>'
            f'<span class="legend-count">{count}</span>'
            f'<span class="legend-pct">{frac*100:.1f}%</span></div>')
    total_label = f'<text x="{cx}" y="{cy-4}" text-anchor="middle" fill="var(--text)" font-size="20" font-weight="600">{total}</text>'
    total_sub = f'<text x="{cx}" y="{cy+14}" text-anchor="middle" fill="var(--muted)" font-size="10">{unit}</text>'
    svg = (f'<svg viewBox="0 0 {cx*2} {cy*2}" style="max-width:220px;width:100%">'
           + "".join(svg_parts) + total_label + total_sub + '</svg>')
    return f'''<div class="section"><h2>{title}</h2>
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


def _sparkline_for_history(history, key):
    vals = [h.get(key, 0) for h in history][-20:]
    if len(vals) < 2:
        return ''
    return sparkline_svg(vals)


COMMON_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0f1115; --panel:rgba(24,27,32,0.72); --border:#23272e;
  --text:#e6e6e6; --muted:#8a8f98; --accent:#60a5fa;
  --glass-blur:20px;
}
[data-theme="light"]{
  --bg:#f6f7f9; --panel:rgba(255,255,255,0.75); --border:#e4e6ea;
  --text:#1a1d21; --muted:#6b7280; --accent:#2563eb;
}
[data-theme="navy"]{
  --bg:#0a1628; --panel:rgba(20,35,60,0.72); --border:#1e3a5f;
  --text:#e0e7ff; --muted:#94a3b8; --accent:#60a5fa;
}
html{min-height:100%}
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;
color:var(--text);margin:0 auto;padding:24px;max-width:1100px;
min-height:100vh;
background:var(--bg);
background-image:
  radial-gradient(circle at 15% -10%, rgba(96,165,250,0.12) 0%, transparent 40%),
  radial-gradient(circle at 85% 0%, rgba(74,222,128,0.10) 0%, transparent 40%);
background-attachment:fixed;transition:background-color .3s,color .3s}
h1{margin:0 0 4px;font-size:22px;letter-spacing:-.3px}
h2{font-size:15px;margin:0 0 12px}
.sub{color:var(--muted);font-size:13px;margin-bottom:24px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.section,.card,.box{background:var(--panel);border:1px solid var(--border);
border-radius:14px;padding:18px;margin-bottom:16px;
backdrop-filter:blur(var(--glass-blur));
-webkit-backdrop-filter:blur(var(--glass-blur));
animation:fadeInUp .5s ease-out both}
.card{padding:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
gap:12px;margin-bottom:24px}
.card .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.card .v{font-size:26px;font-weight:700;margin-top:6px;font-variant-numeric:tabular-nums}
.card .v.good{color:#4ade80}
.card .v.bad{color:#f87171}
.card .v.warn{color:#facc15}
@keyframes fadeInUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border)}
th{color:var(--muted);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
tr:hover td{background:rgba(96,165,250,0.05)}
.tag{display:inline-block;padding:3px 10px;border-radius:20px;background:var(--border);
font-size:12px;color:var(--muted);margin:2px}
.back{display:inline-block;margin-bottom:16px;color:var(--accent);text-decoration:none}
code{background:var(--bg);padding:3px 8px;border-radius:6px;font-size:13px;
word-break:break-all;color:#facc15;font-family:ui-monospace,Menlo,monospace}
.top-right{position:fixed;top:16px;right:16px;z-index:100;display:flex;gap:8px;align-items:center}
.theme-btn{background:var(--panel);border:1px solid var(--border);color:var(--text);
width:42px;height:42px;border-radius:50%;cursor:pointer;font-size:18px;
backdrop-filter:blur(var(--glass-blur));-webkit-backdrop-filter:blur(var(--glass-blur));
transition:transform .15s}
.theme-btn:hover{transform:scale(1.08)}
.live-widget{background:var(--panel);border:1px solid var(--border);
border-radius:20px;padding:8px 14px;font-size:13px;
backdrop-filter:blur(var(--glass-blur));-webkit-backdrop-filter:blur(var(--glass-blur));
display:flex;gap:14px;align-items:center;white-space:nowrap}
.live-widget .live-item{display:flex;align-items:center;gap:4px;color:var(--muted)}
.live-widget .live-item strong{color:var(--text);font-weight:500}
@media(max-width:600px){.live-widget{padding:6px 10px;font-size:12px;gap:10px}}
@media(max-width:420px){.live-widget .hide-mobile{display:none}}
.health-wrap{margin-bottom:24px;padding:14px 18px;background:var(--panel);
border:1px solid var(--border);border-radius:14px;backdrop-filter:blur(var(--glass-blur))}
.health-bar{height:8px;background:var(--border);border-radius:8px;overflow:hidden}
.health-bar > div{height:100%;border-radius:8px;transition:width 1.2s}
.health-label{margin-top:8px;font-size:13px;color:var(--muted)}
.donut-wrap{display:flex;gap:24px;align-items:center;flex-wrap:wrap}
.donut-chart{flex:0 0 220px}
.donut-legend{flex:1;min-width:240px}
.legend-row{display:flex;align-items:center;gap:8px;padding:5px 0;font-size:13px;
border-bottom:1px solid var(--border)}
.legend-row:last-child{border-bottom:none}
.legend-dot{width:10px;height:10px;border-radius:3px;flex:0 0 auto}
.legend-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.legend-count{color:var(--muted);font-size:12px}
.legend-pct{color:var(--text);font-size:12px;font-weight:500;min-width:44px;text-align:right}
.wf-status{font-size:13px;margin-bottom:16px;padding:10px 14px;
background:var(--panel);border:1px solid var(--border);border-radius:12px;
backdrop-filter:blur(var(--glass-blur))}
.copy-btn{background:var(--border);border:none;color:var(--text);padding:6px 12px;
border-radius:8px;font-size:12px;cursor:pointer;transition:background .15s}
.copy-btn:hover{background:var(--accent);color:#fff}
.copy-btn.copied{background:#4ade80;color:#0f1115}
.uptime-good{color:#4ade80;font-weight:500}
.uptime-mid{color:#facc15;font-weight:500}
.uptime-low{color:#f87171;font-weight:500}
.uptime-none{color:var(--muted)}
.reason-badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px;
font-weight:500;background:var(--border);color:var(--muted)}
.reason-badge.dead{background:rgba(248,113,113,0.15);color:#f87171}
.reason-badge.filter{background:rgba(250,204,21,0.15);color:#facc15}
.reason-badge.unstable{background:rgba(250,204,21,0.15);color:#facc15}
.quality-4k{color:#a78bfa;font-weight:600;font-size:11px}
.quality-hd{color:#4ade80;font-weight:600;font-size:11px}
.quality-sd{color:#facc15;font-weight:600;font-size:11px}
.chk{display:inline-flex;align-items:center;gap:6px;padding:8px 14px;
background:var(--panel);border:1px solid var(--border);border-radius:10px;
cursor:pointer;font-size:13px;user-select:none}
.chk:hover{border-color:var(--accent)}
.chk input{accent-color:var(--accent);cursor:pointer}
.chk-bar{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.view-toggle{background:var(--panel);border:1px solid var(--border);color:var(--text);
padding:8px 16px;border-radius:10px;cursor:pointer;font-size:13px;user-select:none}
.view-toggle:hover{border-color:var(--accent)}
.channels-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px}
.channel-card{background:var(--panel);border:1px solid var(--border);
border-radius:14px;padding:14px;text-align:center;position:relative;
backdrop-filter:blur(var(--glass-blur));animation:fadeInUp .4s ease-out both}
.channel-card:hover{border-color:var(--accent)}
.channel-card .cc-logo{width:56px;height:56px;object-fit:contain;
background:var(--border);border-radius:10px;padding:6px;margin-bottom:8px}
.channel-card .cc-name{font-size:13px;font-weight:500;margin-bottom:4px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.channel-card .cc-meta{font-size:11px;color:var(--muted);
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.channel-card .cc-status{position:absolute;top:8px;right:8px;font-size:14px}
.channel-card .cc-actions{margin-top:8px}
.channel-card .cc-btn{background:var(--border);border:none;color:var(--muted);
padding:3px 8px;border-radius:6px;font-size:10px;cursor:pointer}
.channel-card .cc-btn.ok{background:#4ade80;color:#0f1115}
.charts-tip{position:fixed;background:var(--panel);border:1px solid var(--border);
border-radius:10px;padding:8px 12px;font-size:12px;pointer-events:none;
backdrop-filter:blur(var(--glass-blur));box-shadow:0 4px 16px rgba(0,0,0,0.3);
z-index:200;opacity:0;transition:opacity .15s;color:var(--text);white-space:nowrap}
.charts-tip.show{opacity:1}
.charts-tip .tt-val{font-weight:600;color:var(--accent)}
.charts-tip .tt-date{color:var(--muted);font-size:11px;margin-top:2px}
.bg-decor{position:fixed;inset:0;z-index:-1;overflow:hidden;pointer-events:none}
.bg-decor::before{content:'';position:absolute;top:-20%;left:-10%;
width:50vw;height:50vw;border-radius:50%;
background:radial-gradient(circle,rgba(96,165,250,0.08),transparent 60%);
animation:float1 25s ease-in-out infinite}
.bg-decor::after{content:'';position:absolute;bottom:-20%;right:-10%;
width:60vw;height:60vw;border-radius:50%;
background:radial-gradient(circle,rgba(74,222,128,0.06),transparent 60%);
animation:float2 30s ease-in-out infinite}
@keyframes float1{0%,100%{transform:translate(0,0)}50%{transform:translate(60px,40px)}}
@keyframes float2{0%,100%{transform:translate(0,0)}50%{transform:translate(-80px,-50px)}}
.hero{display:grid;grid-template-columns:1fr;gap:16px;margin-bottom:20px}
.clock-card{background:var(--panel);border:1px solid var(--border);
border-radius:24px;padding:32px 24px;text-align:center;
backdrop-filter:blur(var(--glass-blur));position:relative;overflow:hidden;
animation:fadeInUp .9s ease-out}
.clock-card::before{content:'';position:absolute;top:-50%;left:-50%;
width:200%;height:200%;
background:conic-gradient(from 0deg,transparent,rgba(96,165,250,0.06),transparent 25%);
animation:rotate 24s linear infinite;pointer-events:none}
@keyframes rotate{to{transform:rotate(360deg)}}
.clock{font-size:clamp(60px,17vw,100px);font-weight:200;letter-spacing:-0.04em;
line-height:1;font-variant-numeric:tabular-nums;
background:linear-gradient(135deg, var(--accent), #a78bfa);
-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;
position:relative;z-index:1}
.clock .colon{animation:blink 1s steps(1) infinite;opacity:0.7}
@keyframes blink{50%{opacity:0.2}}
.clock .sec{font-size:0.38em;color:var(--accent);-webkit-text-fill-color:var(--accent);
margin-left:10px;opacity:0.7;font-variant-numeric:tabular-nums}
.tz{font-size:13px;color:var(--muted);margin-top:10px;position:relative;z-index:1}
.date-line{margin-top:16px;font-size:16px;position:relative;z-index:1;
display:flex;justify-content:center;align-items:baseline;gap:10px;flex-wrap:wrap}
.date-line .wd{color:var(--accent);font-weight:500}
.date-line .yr{color:#a78bfa;font-weight:500;font-size:20px;font-variant-numeric:tabular-nums}
.date-line .mo{color:var(--muted);font-size:14px}
.art-card{border-radius:24px;overflow:hidden;aspect-ratio:3/2;
background:var(--panel);border:1px solid var(--border);position:relative;
animation:fadeInUp .9s ease-out .1s both}
.art-card svg{width:100%;height:100%;display:block}
.art-label{position:absolute;bottom:12px;right:14px;font-size:11px;
color:rgba(255,255,255,0.65);background:rgba(0,0,0,0.35);
padding:5px 12px;border-radius:14px;backdrop-filter:blur(8px)}
.holiday-plaque{text-align:center;padding:10px 20px;
background:linear-gradient(135deg,rgba(255,193,7,0.15),rgba(255,87,34,0.15));
border:1px solid rgba(255,193,7,0.3);border-radius:14px;margin-bottom:16px;
font-size:15px;font-weight:500;animation:holidayPulse 3s ease-in-out infinite}
@keyframes holidayPulse{0%,100%{box-shadow:0 0 0 0 rgba(255,193,7,0.3)}
50%{box-shadow:0 0 30px 4px rgba(255,193,7,0.2)}}
.info-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:20px}
@media(max-width:520px){.info-grid{grid-template-columns:1fr}}
.wx-hero{display:flex;align-items:center;justify-content:center;gap:16px;margin-bottom:14px}
.wx-icon{font-size:64px;line-height:1;filter:drop-shadow(0 4px 16px rgba(96,165,250,0.3))}
.wx-temp{font-size:46px;font-weight:200;letter-spacing:-0.02em;line-height:1}
.wx-feels{font-size:12px;color:var(--muted);margin-top:4px}
.wx-meta{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;
padding-top:12px;border-top:1px solid var(--border)}
.wx-meta-item{text-align:center;font-size:11px}
.wx-meta-item span{display:block;font-size:18px;margin-bottom:2px}
.wx-meta-item b{font-size:13px;font-weight:500}
.wx-meta-item small{color:var(--muted);display:block;font-size:10px;margin-top:2px}
.wx-sun{display:flex;justify-content:space-around;padding:12px 0;margin-top:12px;
border-top:1px solid var(--border);font-size:12px;color:var(--muted)}
.wx-sun-item{display:flex;align-items:center;gap:6px}
.wx-sun-item b{color:var(--text);font-weight:500}
.wx-forecast{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;
margin-top:12px;padding-top:12px;border-top:1px solid var(--border)}
.fc-day{text-align:center}
.fc-date{font-size:10px;color:var(--muted);margin-bottom:6px}
.fc-icon{font-size:26px;margin-bottom:6px;line-height:1}
.fc-temps{font-size:11px;display:flex;flex-direction:column;gap:2px}
.fc-max{color:var(--text);font-weight:500}
.fc-min{color:var(--muted);font-size:10px}
.cur-row{display:flex;align-items:center;gap:10px;padding:8px 0;font-size:14px}
.cur-row + .cur-row{border-top:1px solid var(--border)}
.cur-flag{font-size:20px}
.cur-name{font-size:12px;color:var(--muted);min-width:38px;letter-spacing:0.5px}
.cur-val{margin-left:auto;font-variant-numeric:tabular-nums;font-weight:500;font-size:16px}
.moon-wrap{text-align:center;padding:8px 0}
.moon-icon{font-size:72px;line-height:1;margin-bottom:10px;display:block;
filter:drop-shadow(0 0 24px rgba(167,139,250,0.5));
animation:moonGlow 4s ease-in-out infinite}
@keyframes moonGlow{0%,100%{filter:drop-shadow(0 0 20px rgba(167,139,250,0.4))}
50%{filter:drop-shadow(0 0 32px rgba(167,139,250,0.6))}}
.moon-name{font-size:13px;color:var(--muted)}
.cal-month{font-size:15px;font-weight:500;text-align:center;margin-bottom:12px}
.cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:3px}
.cal-head{font-size:10px;color:var(--muted);text-align:center;font-weight:600;
padding:4px 0;letter-spacing:0.5px}
.cal-cell{aspect-ratio:1;display:flex;align-items:center;justify-content:center;
font-size:12px;border-radius:8px;position:relative;font-variant-numeric:tabular-nums;
transition:background .2s}
.cal-cell:hover{background:rgba(96,165,250,0.1)}
.cal-empty{opacity:0.2}
.cal-weekend{color:var(--muted)}
.cal-today{background:linear-gradient(135deg,var(--accent),#a78bfa);color:white;
font-weight:600;box-shadow:0 0 16px rgba(96,165,250,0.4)}
.cal-hol{position:absolute;top:2px;right:3px;font-size:8px;color:#f87171;line-height:1}
.quote-text{font-size:16px;font-style:italic;line-height:1.55;margin-bottom:12px}
.quote-author{font-size:12px;color:var(--muted);text-align:right}
.quote-author::before{content:'— '}
.news-item{display:block;padding:10px 0;color:var(--text);text-decoration:none;
font-size:13px;line-height:1.4;border-bottom:1px solid var(--border);
transition:transform .15s}
.news-item:last-child{border-bottom:none}
.news-item:hover{transform:translateX(4px)}
.news-title{font-weight:500;margin-bottom:4px;transition:color .15s}
.news-item:hover .news-title{color:var(--accent)}
.news-desc{font-size:11px;color:var(--muted);line-height:1.5}
.counter{text-align:center;padding:6px 0}
.counter-num{font-size:60px;font-weight:200;letter-spacing:-0.03em;
background:linear-gradient(135deg, #f87171, #facc15);
-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;
line-height:1}
.counter-label{font-size:12px;color:var(--muted);margin-top:8px}
.secure-block{background:var(--panel);border:1px solid var(--border);
border-radius:16px;padding:24px;margin-bottom:24px;
backdrop-filter:blur(var(--glass-blur));animation:fadeInUp .5s ease-out both}
.secure-locked{text-align:center}
.secure-icon{font-size:48px;line-height:1;margin-bottom:12px}
.secure-title{font-size:16px;font-weight:500;margin-bottom:6px}
.secure-hint{font-size:13px;color:var(--muted);margin-bottom:20px}
.secure-input-wrap{display:flex;gap:8px;max-width:400px;margin:0 auto}
.secure-input-wrap input{flex:1;background:var(--bg);border:1px solid var(--border);
color:var(--text);padding:12px 16px;border-radius:10px;font-size:14px;font-family:inherit}
.secure-input-wrap input:focus{outline:none;border-color:var(--accent)}
.secure-btn{background:var(--accent);border:none;color:#fff;padding:12px 24px;
border-radius:10px;font-size:14px;font-weight:500;cursor:pointer;
transition:transform .15s;white-space:nowrap}
.secure-btn:hover{transform:translateY(-1px)}
.secure-btn:disabled{opacity:0.5;cursor:wait}
.secure-error{color:#f87171;font-size:13px;margin-top:12px;min-height:18px}
.secure-unlocked{text-align:center}
.secure-timer{font-size:13px;color:var(--muted);margin-bottom:16px;padding:8px 16px;
background:var(--bg);border-radius:20px;display:inline-block}
.secure-timer b{color:var(--accent);font-variant-numeric:tabular-nums}
.secure-main{display:flex;gap:20px;align-items:center;flex-wrap:wrap;justify-content:center}
.secure-qr{background:#fff;padding:10px;border-radius:12px}
.secure-qr canvas,.secure-qr img{display:block;width:180px;height:180px}
.secure-links{flex:1;min-width:240px;text-align:left;
display:flex;flex-direction:column;gap:10px}
.secure-label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px}
.link-line{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.splits-list{margin-top:20px;padding-top:16px;border-top:1px solid var(--border);
text-align:left}
.splits-list h4{font-size:11px;color:var(--muted);text-transform:uppercase;
letter-spacing:1px;margin-bottom:12px;font-weight:600}
.split-item{display:flex;align-items:center;gap:10px;padding:8px 10px;
background:var(--bg);border-radius:10px;margin-bottom:6px;font-size:13px}
.split-name{flex:1;font-weight:500}
.split-count{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
.split-copy{background:var(--border);border:none;color:var(--muted);
padding:5px 10px;border-radius:6px;font-size:11px;cursor:pointer}
.split-copy:hover{background:var(--accent);color:#fff}
.split-copy.ok{background:#4ade80;color:#0f1115}
.secure-btn-close{background:transparent;border:1px solid var(--border);
color:var(--muted);padding:8px 16px;border-radius:10px;font-size:13px;
cursor:pointer;align-self:flex-start;margin-top:12px}
.secure-btn-close:hover{border-color:var(--accent);color:var(--accent)}
.splash{position:fixed;inset:0;background:var(--bg);z-index:1000;
display:flex;align-items:center;justify-content:center;font-size:60px;
animation:splashOut 0.9s ease-out 1.2s forwards;pointer-events:none}
@keyframes splashOut{to{opacity:0;visibility:hidden}}
#bgCanvas{position:fixed;top:0;left:0;width:100%;height:100%;z-index:0;
pointer-events:none;opacity:0.65}
.wrap{max-width:1100px;margin:0 auto;position:relative;z-index:1}
"""

THEME_JS = """
<script>
(function(){
  const THEMES = ['dark', 'light', 'navy'];
  const ICONS = {dark: '☀', light: '☾', navy: '🌙'};
  let saved = localStorage.getItem('theme') || 'dark';
  if (!THEMES.includes(saved)) saved = 'dark';
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

  function updateClock(){
    const el = document.getElementById('clock');
    if (!el) return;
    try {
      const now = new Date();
      const t = now.toLocaleTimeString('ru-RU', {
        timeZone: 'Europe/Moscow', hour: '2-digit', minute: '2-digit'
      });
      el.innerHTML = '🕐 <strong>' + t + '</strong> <span class="hide-mobile">МСК</span>';
    } catch(e) {}
  }
  updateClock();
  setInterval(updateClock, 1000);

  function updateTicker(){
    const el = document.getElementById('ticker');
    if (!el) return;
    const gh = el.dataset.repo;
    if (!gh) { el.innerHTML = ''; return; }
    fetch('https://api.github.com/repos/' + gh + '/actions/workflows/check.yml/runs?per_page=1')
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if(!d || !d.workflow_runs || !d.workflow_runs.length) return;
        const run = d.workflow_runs[0];
        const when = new Date(run.updated_at || run.created_at);
        const ago = Math.round((Date.now() - when.getTime())/60000);
        let agoStr = ago < 1 ? 'только что'
                   : ago < 60 ? ago + ' мин назад'
                   : Math.round(ago/60) + ' ч назад';
        let status = '';
        if(run.status === 'in_progress') status = '🔄';
        else if(run.conclusion === 'success') status = '✅';
        else if(run.conclusion === 'failure') status = '❌';
        else status = '⏸';
        const nextIn = 360 - ago;
        let nextStr = '';
        if (nextIn > 0) {
          const h = Math.floor(nextIn/60), m = nextIn % 60;
          nextStr = ' · <span class="hide-mobile">след. через ' + (h>0? h+'ч ':'') + m + 'м</span>';
        }
        el.innerHTML = status + ' <strong>' + agoStr + '</strong>' + nextStr;
      })
      .catch(() => {});
  }
  updateTicker();
  setInterval(updateTicker, 60000);

  document.addEventListener('DOMContentLoaded', function(){
    const btn = document.querySelector('.theme-btn');
    if(btn){
      btn.textContent = ICONS[saved];
      btn.addEventListener('click', function(){
        const cur = document.documentElement.getAttribute('data-theme') || 'dark';
        const idx = THEMES.indexOf(cur);
        const nxt = THEMES[(idx + 1) % THEMES.length];
        document.documentElement.setAttribute('data-theme', nxt);
        localStorage.setItem('theme', nxt);
        btn.textContent = ICONS[nxt];
      });
    }
    animateCounters();
  });

  window.showChartTip = function(e, text, date){
    let tip = document.getElementById('chart-tip');
    if(!tip){
      tip = document.createElement('div');
      tip.id = 'chart-tip';
      tip.className = 'charts-tip';
      document.body.appendChild(tip);
    }
    tip.innerHTML = '<div class="tt-val">' + text + '</div>' +
                    (date ? '<div class="tt-date">' + date + '</div>' : '');
    tip.classList.add('show');
    tip.style.left = (e.clientX + 12) + 'px';
    tip.style.top = (e.clientY - 40) + 'px';
  };
  window.hideChartTip = function(){
    const tip = document.getElementById('chart-tip');
    if(tip) tip.classList.remove('show');
  };
})();
</script>
"""

TOP_RIGHT_WIDGET = '''<div class="top-right">
<div class="live-widget">
  <span class="live-item">{weather_html}</span>
  <span class="live-item" id="clock">🕐 <span class="hide-mobile">МСК</span></span>
  <span class="live-item" id="ticker" data-repo="{github_repo}">⏳</span>
</div>
<button class="theme-btn" aria-label="Theme">☀</button>
</div>
<div class="bg-decor"></div>'''


REPORT_T = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>M3U Check - {date}</title>
<link rel="icon" type="image/svg+xml" href="icon.svg">
<style>{common_css}</style>
</head><body>
{top_right}
<a class="back" href="index.html">← на главную</a>
<h1>Отчёт проверки</h1>
<div class="sub">{date} · источников: {n_playlists}</div>
{health_block}
<div class="cards">
<div class="card"><div class="k">Проверено</div><div class="v" data-count="{total}">0</div></div>
<div class="card"><div class="k">Рабочих</div><div class="v good" data-count="{ok}">0</div></div>
<div class="card"><div class="k">Нестабильных</div><div class="v warn" data-count="{unstable}">0</div></div>
<div class="card"><div class="k">Отфильтровано</div><div class="v warn" data-count="{filtered}">0</div></div>
<div class="card"><div class="k">Мёртвых</div><div class="v bad" data-count="{dead}">0</div></div>
<div class="card"><div class="k">В merged</div><div class="v" data-count="{merged}">0</div></div>
</div>
{quality_block}
{donut_block}
{history_block}
{unstable_block}
{logo_block}
{ua_block}
{slow_block}
{ffprobe_block}
<div class="section"><h2>По источникам</h2>
<table><thead><tr><th>Источник</th><th>Всего</th><th>Рабочих</th><th>Нестаб.</th><th>Фильтр</th><th>%</th><th>Ср. отклик</th></tr></thead>
<tbody>{playlists_rows}</tbody></table></div>
<div class="section"><h2>Фильтры</h2>
<table><thead><tr><th>Причина</th><th>Каналов</th></tr></thead>
<tbody>{filter_rows}</tbody></table></div>
<div class="box"><a href="rejected.html">🚫 Отсеянные каналы</a> ·
<a href="rejected.csv">📥 CSV</a></div>
{theme_js}
</body></html>
"""


INDEX_T_SECURE = """<!DOCTYPE html>
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
<meta name="robots" content="noindex,nofollow">
<script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>
<script>window.QRCode||document.write('<script src="https://unpkg.com/qrcodejs@1.0.0/qrcode.min.js"><\\/script>')</script>
<style>{common_css}</style></head><body>
<div class="splash">✨</div>
<canvas id="bgCanvas"></canvas>
{top_right}
<div class="wrap">

{holiday_plaque}

<div class="hero">
  <div class="clock-card">
    <div class="clock">
      <span id="h1">-</span><span id="h2">-</span><span class="colon">:</span><span id="m1">-</span><span id="m2">-</span><span class="sec" id="ss">--</span>
    </div>
    <div class="tz">Московское время · UTC+3</div>
    <div class="date-line">
      <span class="wd">{weekday}</span>
      <span class="yr">{year_num}</span>
      <span class="mo">{date_short} · {month_name}</span>
    </div>
  </div>
  <div class="art-card">
    {svg_art}
    <div class="art-label">🎨 сгенерировано</div>
  </div>
</div>

<h2>Ссылка для плеера</h2>
{secure_block}

<div class="info-grid">
  <div class="card" style="grid-column:1/-1">
    <h3 style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:14px">🌤 Погода · Нальчик</h3>
    {weather_block}
  </div>
  <div class="card">
    <h3 style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:14px">📅 {month_name} {year_num}</h3>
    {calendar_block}
  </div>
  <div class="card">
    <h3 style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:14px">🌕 Фаза луны</h3>
    <div class="moon-wrap">
      <span class="moon-icon">{moon_icon}</span>
      <div class="moon-name">{moon_name}</div>
    </div>
  </div>
  <div class="card">
    <h3 style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:14px">💱 Курсы ЦБ РФ</h3>
    {currency_block}
  </div>
  <div class="card">
    <h3 style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:14px">🎄 До Нового года</h3>
    <div class="counter">
      <div class="counter-num">{days_ny}</div>
      <div class="counter-label">дней осталось</div>
    </div>
  </div>
  <div class="card" style="grid-column:1/-1">
    <h3 style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:14px">💭 Цитата дня</h3>
    <div class="quote-text">«{quote}»</div>
    <div class="quote-author">{quote_author}</div>
  </div>
  <div class="card" style="grid-column:1/-1">
    <h3 style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:14px">📰 Новости</h3>
    {news_block}
  </div>
</div>

{trend_block}
<h2>Разделы</h2>
<div class="box"><a href="report.html">📊 Отчёт проверки</a></div>
<div class="box"><a href="channels.html">🔍 Поиск по каналам</a></div>
<div class="box"><a href="rejected.html">🚫 Отсеянные каналы</a></div>
<div class="box"><a href="channels.csv">📥 CSV (все каналы)</a></div>
<h2>Статистика</h2>
<div class="box">
Каналов в merged: <b>{merged_count}</b><br>
Рабочих: <b>{ok}</b> из <b>{total}</b><br>
Нестабильных отсеяно: <b>{unstable}</b>
</div>

</div>
<script>
(function(){
  const gh = "{github_repo}";
  if(gh){
    fetch('https://api.github.com/repos/' + gh + '/actions/workflows/check.yml/runs?per_page=1')
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if(!d || !d.workflow_runs || !d.workflow_runs.length) return;
        const run = d.workflow_runs[0];
        const el = document.getElementById('wf-status');
        if(!el) return;
        const when = new Date(run.updated_at || run.created_at);
        const ago = Math.round((Date.now() - when.getTime())/60000);
        const agoStr = ago < 1 ? 'только что' : ago < 60 ? ago + ' мин назад' : Math.round(ago/60) + ' ч назад';
        let html = '';
        if(run.status === 'in_progress') html = '<span style="color:#facc15">🔄 Идёт проверка…</span>';
        else if(run.conclusion === 'success') html = '<span style="color:#4ade80">✅ Успешно</span>';
        else if(run.conclusion === 'failure') html = '<span style="color:#f87171">❌ Упало</span>';
        else html = '<span style="color:var(--muted)">⏸</span>';
        html += ' <span style="color:var(--muted);font-size:12px">· ' + agoStr + '</span>';
        el.innerHTML = html;
      }).catch(() => {});
  }

  function tick(){
    const now = new Date();
    const msk = new Date(now.toLocaleString('en-US', {timeZone:'Europe/Moscow'}));
    const hh = String(msk.getHours()).padStart(2, '0');
    const mm = String(msk.getMinutes()).padStart(2, '0');
    const ss = String(msk.getSeconds()).padStart(2, '0');
    const h1 = document.getElementById('h1');
    const h2 = document.getElementById('h2');
    const m1 = document.getElementById('m1');
    const m2 = document.getElementById('m2');
    const sEl = document.getElementById('ss');
    if(h1) h1.textContent = hh[0];
    if(h2) h2.textContent = hh[1];
    if(m1) m1.textContent = mm[0];
    if(m2) m2.textContent = mm[1];
    if(sEl) sEl.textContent = ss;
  }
  tick();
  setInterval(tick, 1000);

  const kind = "{weather_kind}";
  const canvas = document.getElementById('bgCanvas');
  if(canvas){
    const ctx = canvas.getContext('2d');
    let W, H, particles = [];
    function resize(){
      W = canvas.width = window.innerWidth * devicePixelRatio;
      H = canvas.height = window.innerHeight * devicePixelRatio;
      canvas.style.width = window.innerWidth + 'px';
      canvas.style.height = window.innerHeight + 'px';
    }
    resize();
    window.addEventListener('resize', resize);
    let count = 0;
    if(kind === 'rain' || kind === 'storm') count = 100;
    else if(kind === 'snow') count = 70;
    else if(kind === 'sun') count = 40;
    else count = 25;
    for(let i = 0; i < count; i++){
      particles.push({
        x: Math.random() * W,
        y: Math.random() * H,
        vx: kind === 'rain' ? 0.5 : (Math.random() - 0.5) * 0.6,
        vy: kind === 'rain' ? 4 + Math.random() * 6
           : kind === 'snow' ? 0.4 + Math.random() * 1
           : (Math.random() - 0.5) * 0.5,
        r: kind === 'rain' ? 1 + Math.random() * 1.5
          : kind === 'snow' ? 2 + Math.random() * 3
          : 1 + Math.random() * 2.5,
        a: 0.15 + Math.random() * 0.5,
      });
    }
    function draw(){
      ctx.clearRect(0, 0, W, H);
      for(const p of particles){
        if(kind === 'rain' || kind === 'storm'){
          ctx.strokeStyle = 'rgba(150,200,255,' + p.a + ')';
          ctx.lineWidth = p.r;
          ctx.beginPath();
          ctx.moveTo(p.x, p.y);
          ctx.lineTo(p.x + p.vx * 3, p.y + 20 + p.vy * 2);
          ctx.stroke();
        } else if(kind === 'snow'){
          ctx.fillStyle = 'rgba(255,255,255,' + p.a + ')';
          ctx.beginPath();
          ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
          ctx.fill();
        } else if(kind === 'sun'){
          ctx.fillStyle = 'rgba(255,220,120,' + (p.a * 0.8) + ')';
          ctx.beginPath();
          ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
          ctx.fill();
        } else {
          ctx.fillStyle = 'rgba(200,220,255,' + (p.a * 0.6) + ')';
          ctx.beginPath();
          ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
          ctx.fill();
        }
        p.x += p.vx;
        p.y += p.vy;
        if(p.y > H + 20){ p.y = -20; p.x = Math.random() * W; }
        if(p.x > W + 20) p.x = -20;
        if(p.x < -20) p.x = W + 20;
      }
      requestAnimationFrame(draw);
    }
    draw();
  }

  const secureBlock = document.getElementById('secureBlock');
  const encBlob = secureBlock ? secureBlock.dataset.enc : '';
  const input = document.getElementById('pwdInput');
  const btn = document.getElementById('pwdBtn');
  const errEl = document.getElementById('pwdError');
  const locked = document.getElementById('secureLocked');
  const unlocked = document.getElementById('secureUnlocked');
  const timerEl = document.getElementById('timerVal');
  let countdownTimer = null;

  if(!encBlob){
    if(locked){
      locked.innerHTML = '<div class="secure-icon">⚠️</div><div class="secure-title">Раздел временно недоступен</div>';
    }
    return;
  }

  function base64ToBytes(b64){
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for(let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }

  async function deriveKey(password, salt){
    const enc = new TextEncoder();
    const baseKey = await crypto.subtle.importKey(
      'raw', enc.encode(password), 'PBKDF2', false, ['deriveKey']);
    return crypto.subtle.deriveKey(
      {name:'PBKDF2', salt: salt, iterations: 150000, hash: 'SHA-256'},
      baseKey, {name:'AES-GCM', length:256}, false, ['decrypt']);
  }

  async function decrypt(blob, password){
    const raw = base64ToBytes(blob);
    const salt = raw.slice(0, 16);
    const iv = raw.slice(16, 28);
    const data = raw.slice(28);
    const key = await deriveKey(password, salt);
    const plain = await crypto.subtle.decrypt({name:'AES-GCM', iv: iv}, key, data);
    return JSON.parse(new TextDecoder().decode(plain));
  }

  function startHideTimer(seconds){
    let left = seconds;
    function t(){
      const m = Math.floor(left/60), s = left % 60;
      if(timerEl) timerEl.textContent = m + ':' + String(s).padStart(2, '0');
      if(left <= 0){ hideNow(); return; }
      left -= 1;
    }
    t();
    countdownTimer = setInterval(t, 1000);
  }

  function hideNow(){
    if(countdownTimer) clearInterval(countdownTimer);
    if(unlocked) unlocked.style.display = 'none';
    if(locked) locked.style.display = 'block';
    if(input) input.value = '';
    if(errEl) errEl.textContent = '';
    const qrWrap = document.getElementById('qrWrap');
    if(qrWrap) qrWrap.innerHTML = '';
  }

  const closeBtn = document.getElementById('closeBtn');
  if(closeBtn) closeBtn.addEventListener('click', hideNow);

  function escapeHtml(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  function escapeAttr(s){ return String(s).replace(/"/g, '&quot;'); }

  if(btn) btn.addEventListener('click', async () => {
    const pwd = input.value.trim();
    if(!pwd){ errEl.textContent = 'Введите пароль'; return; }
    btn.disabled = true;
    errEl.textContent = 'Расшифровка...';
    try {
      const data = await decrypt(encBlob, pwd);
      const pl = document.getElementById('playerUrlLocked');
      if(pl) pl.textContent = data.playlist;

      const qrWrap = document.getElementById('qrWrap');
      qrWrap.innerHTML = '';
      if(window.QRCode){
        new QRCode(qrWrap, {
          text: data.playlist, width: 180, height: 180,
          colorDark: '#000', colorLight: '#fff',
          correctLevel: QRCode.CorrectLevel.M,
        });
      }

      const splitsWrap = document.getElementById('splitsWrap');
      if(splitsWrap && data.splits && data.splits.length){
        const base = data.base_url || '';
        splitsWrap.innerHTML = '<h4>📂 Разделы</h4>' + data.splits.map(s => {
          const url = base + s.file;
          return '<div class="split-item">' +
            '<span class="split-name">' + escapeHtml(s.name) + '</span>' +
            '<span class="split-count">' + s.count + '</span>' +
            '<button class="split-copy" data-u="' + escapeAttr(url) + '">📋</button>' +
            '</div>';
        }).join('');
        splitsWrap.querySelectorAll('.split-copy').forEach(b => {
          b.addEventListener('click', () => {
            navigator.clipboard.writeText(b.dataset.u).then(() => {
              b.classList.add('ok');
              const t = b.textContent; b.textContent = '✓';
              setTimeout(() => { b.classList.remove('ok'); b.textContent = t; }, 1200);
            });
          });
        });
      } else if(splitsWrap){
        splitsWrap.innerHTML = '';
      }

      locked.style.display = 'none';
      unlocked.style.display = 'block';
      errEl.textContent = '';
      startHideTimer(300);
    } catch(e) {
      errEl.textContent = '❌ Неверный пароль';
    } finally {
      btn.disabled = false;
    }
  });

  if(input) input.addEventListener('keydown', (e) => {
    if(e.key === 'Enter') btn.click();
  });

  document.querySelectorAll('.copy-btn[data-target]').forEach(b => {
    b.addEventListener('click', () => {
      const el = document.getElementById(b.dataset.target);
      if(!el || el.textContent === '—') return;
      navigator.clipboard.writeText(el.textContent).then(() => {
        b.classList.add('copied');
        const t = b.textContent; b.textContent = '✓';
        setTimeout(() => { b.classList.remove('copied'); b.textContent = t; }, 1200);
      });
    });
  });
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
input[type=search],select{background:var(--panel);border:1px solid var(--border);
color:var(--text);padding:10px 12px;border-radius:10px;font-size:14px;width:100%;
margin-bottom:10px;font-family:inherit}
.controls{display:grid;grid-template-columns:1fr 220px;gap:10px;margin-bottom:16px}
@media(max-width:600px){.controls{grid-template-columns:1fr}}
th{position:sticky;top:0;background:var(--bg);z-index:2}
.logo{width:32px;height:32px;object-fit:contain;vertical-align:middle;
background:var(--border);border-radius:6px;padding:3px}
.name{font-weight:500}
.group{color:var(--muted);font-size:12px}
.copy{background:var(--border);border:none;color:var(--muted);padding:5px 10px;
border-radius:6px;font-size:11px;cursor:pointer}
.copy:hover{background:var(--accent);color:#fff}
.copy.ok{background:#4ade80;color:#0f1115}
.view-bar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
</style></head><body>
{top_right}
<a href="index.html">← на главную</a>
<h1>Каналы</h1>
<div class="sub">Всего: <b>{total}</b> · с EPG: <b>{epg}</b> · показано: <span id="shown">{total}</span></div>
<div class="chk-bar">
<label class="chk"><input type="checkbox" id="f-epg"> Только с EPG</label>
<label class="chk"><input type="checkbox" id="f-logo"> С логотипом</label>
<label class="chk"><input type="checkbox" id="f-stable"> Стабильные &gt;80%</label>
<label class="chk"><input type="checkbox" id="f-4k"> Только 4K</label>
<label class="chk"><input type="checkbox" id="f-hd"> Только HD</label>
</div>
<div class="view-bar">
<button class="view-toggle" id="view-table">📋 Таблица</button>
<button class="view-toggle" id="view-grid">🔲 Карточки</button>
</div>
<div class="controls">
<input id="q" type="search" placeholder="Поиск по названию...">
<select id="g"><option value="">Все группы</option>{group_options}</select>
</div>
<div id="table-view">
<table><thead><tr><th></th><th>Название</th><th>Кач.</th><th>Аптайм</th><th>Группа</th><th></th></tr></thead>
<tbody id="tb"></tbody></table>
</div>
<div id="grid-view" style="display:none">
<div class="channels-grid" id="grid"></div>
</div>
<script>
const CH = {channels_json};
const tb = document.getElementById('tb');
const grid = document.getElementById('grid');
const tableWrap = document.getElementById('table-view');
const gridWrap = document.getElementById('grid-view');
const q = document.getElementById('q');
const g = document.getElementById('g');
const fEpg = document.getElementById('f-epg');
const fLogo = document.getElementById('f-logo');
const fStable = document.getElementById('f-stable');
const f4k = document.getElementById('f-4k');
const fHd = document.getElementById('f-hd');
const shown = document.getElementById('shown');
let viewMode = localStorage.getItem('ch-view') || 'table';

function esc(s){ return (s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function uptimeHtml(pct, samples) {
  if (pct === null || pct === undefined || samples < 2) return '<span class="uptime-none">—</span>';
  let cls = pct < 50 ? 'uptime-low' : pct < 80 ? 'uptime-mid' : 'uptime-good';
  return '<span class="' + cls + '">' + pct + '%</span>';
}
function qualityHtml(q) {
  if (q === '4K') return '<span class="quality-4k">4K</span>';
  if (q === 'HD') return '<span class="quality-hd">HD</span>';
  if (q === 'SD') return '<span class="quality-sd">SD</span>';
  return '—';
}
function statusEmoji(pct, samples) {
  if (pct === null || pct === undefined || samples < 2) return '⚪';
  if (pct >= 90) return '🟢';
  if (pct >= 50) return '🟡';
  return '🔴';
}
function filtered() {
  const term = q.value.trim().toLowerCase();
  const grp = g.value;
  let out = [];
  for (const c of CH) {
    if (grp && c.group !== grp) continue;
    if (term && !c.name.toLowerCase().includes(term)) continue;
    if (fEpg.checked && !c.tvg_id) continue;
    if (fLogo.checked && !c.logo) continue;
    if (fStable.checked) {
      if (c.uptime_pct === null || c.uptime_pct === undefined || c.uptime_pct < 80) continue;
    }
    if (f4k.checked && c.quality !== '4K') continue;
    if (fHd.checked && c.quality !== 'HD') continue;
    out.push(c);
  }
  return out.slice(0, 800);
}
function renderTable(out) {
  tb.innerHTML = out.map(c => {
    const logo = c.logo ? '<img class="logo" src="' + esc(c.logo) + '" loading="lazy" onerror="this.style.display=\\'none\\'">' : '';
    const epg = c.tvg_id ? '<span style="color:#4ade80;font-size:11px;margin-left:4px">EPG</span>' : '';
    return '<tr><td>' + logo + '</td>' +
      '<td class="name">' + esc(c.name) + epg + '</td>' +
      '<td>' + qualityHtml(c.quality) + '</td>' +
      '<td>' + uptimeHtml(c.uptime_pct, c.uptime_samples) + '</td>' +
      '<td class="group">' + esc(c.group) + '</td>' +
      '<td><button class="copy" data-u="' + esc(c.url) + '">URL</button></td></tr>';
  }).join('') || '<tr><td colspan="6" style="text-align:center;padding:24px">Ничего не найдено</td></tr>';
  tb.querySelectorAll('.copy').forEach(b => b.addEventListener('click', () => {
    navigator.clipboard.writeText(b.dataset.u).then(() => {
      b.classList.add('ok'); const t = b.textContent; b.textContent = '✓';
      setTimeout(() => { b.classList.remove('ok'); b.textContent = t; }, 1000);
    });
  }));
}
function renderGrid(out) {
  grid.innerHTML = out.map((c, i) => {
    const logo = c.logo ? '<img class="cc-logo" src="' + esc(c.logo) + '" loading="lazy">' : '';
    const st = statusEmoji(c.uptime_pct, c.uptime_samples);
    const q = c.quality && c.quality !== 'Unknown' ? ' · ' + c.quality : '';
    return '<div class="channel-card" style="animation-delay:' + (i * 0.01) + 's">' +
      '<span class="cc-status">' + st + '</span>' + logo +
      '<div class="cc-name">' + esc(c.name) + '</div>' +
      '<div class="cc-meta">' + esc(c.group) + q + '</div>' +
      '<div class="cc-actions"><button class="cc-btn copy" data-u="' + esc(c.url) + '">URL</button></div></div>';
  }).join('') || '<div style="text-align:center;padding:24px;grid-column:1/-1">Ничего не найдено</div>';
  grid.querySelectorAll('.copy').forEach(b => b.addEventListener('click', (e) => {
    e.stopPropagation();
    navigator.clipboard.writeText(b.dataset.u).then(() => {
      b.classList.add('ok'); const t = b.textContent; b.textContent = '✓';
      setTimeout(() => { b.classList.remove('ok'); b.textContent = t; }, 1000);
    });
  }));
}
function render() {
  const out = filtered();
  if (viewMode === 'grid') {
    tableWrap.style.display = 'none'; gridWrap.style.display = 'block';
    renderGrid(out);
  } else {
    tableWrap.style.display = 'block'; gridWrap.style.display = 'none';
    renderTable(out);
  }
  shown.textContent = out.length;
}
document.getElementById('view-table').addEventListener('click', () => {
  viewMode = 'table'; localStorage.setItem('ch-view', 'table'); render();
});
document.getElementById('view-grid').addEventListener('click', () => {
  viewMode = 'grid'; localStorage.setItem('ch-view', 'grid'); render();
});
q.addEventListener('input', render);
g.addEventListener('change', render);
fEpg.addEventListener('change', render);
fLogo.addEventListener('change', render);
fStable.addEventListener('change', render);
f4k.addEventListener('change', render);
fHd.addEventListener('change', render);
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
padding:10px 12px;border-radius:10px;font-size:14px;width:100%;margin-bottom:10px;font-family:inherit}
.controls{display:grid;grid-template-columns:1fr 260px;gap:10px;margin-bottom:16px}
@media(max-width:600px){.controls{grid-template-columns:1fr}}
th{position:sticky;top:0;background:var(--bg);z-index:2}
.name{font-weight:500}
.group{color:var(--muted);font-size:12px}
.url-cell{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--muted);
max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
</style></head><body>
{top_right}
<a href="index.html">← на главную</a>
<h1>Отсеянные каналы</h1>
<div class="sub">Всего: <span id="cnt">{total}</span> · показано: <span id="shown">{total}</span></div>
<div class="controls">
<input id="q" type="search" placeholder="Поиск по названию или URL...">
<select id="r"><option value="">Все причины</option>{reason_options}</select>
</div>
<table><thead><tr><th>Название</th><th>Группа</th><th>URL</th><th>Причина</th></tr></thead>
<tbody id="tb"></tbody></table>
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
  out = out.slice(0, 800);
  tb.innerHTML = out.map(c => '<tr>' +
    '<td class="name">' + esc(c.name) + '</td>' +
    '<td class="group">' + esc(c.group) + '</td>' +
    '<td class="url-cell" title="' + esc(c.url) + '">' + esc(c.url) + '</td>' +
    '<td><span class="' + reasonClass(c.reason) + '">' + esc(c.reason) + '</span></td></tr>'
  ).join('') || '<tr><td colspan="4" style="text-align:center;padding:24px">Ничего не найдено</td></tr>';
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
<stop offset="0" stop-color="#4ade80"/><stop offset="1" stop-color="#22d3ee"/>
</linearGradient>
<radialGradient id="bg" cx="0.3" cy="0.1" r="1">
<stop offset="0" stop-color="#1a2332"/><stop offset="1" stop-color="#0f1115"/>
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
  "name": "IPTV Auto", "short_name": "IPTV",
  "start_url": "index.html", "display": "standalone",
  "background_color": "#0f1115", "theme_color": "#0f1115",
  "icons": [
    {"src": "icon.svg", "sizes": "192x192", "type": "image/svg+xml", "purpose": "any"},
    {"src": "icon.svg", "sizes": "512x512", "type": "image/svg+xml", "purpose": "any"}
  ]
}
"""


def render_trend_svg(history):
    if not history or len(history) < 2:
        return '<div class="section"><h2>Тренд</h2><p style="color:var(--muted)">Данных пока нет.</p></div>'
    W, H, P = 900, 240, 40
    n = len(history)
    step = (W - 2 * P) / max(n - 1, 1)

    def line_points(key):
        vals = [h.get(key, 0) for h in history]
        mx = max(vals) if vals else 1
        mx = max(mx, 1)
        pts = []
        for i, v in enumerate(vals):
            x = P + i * step
            y = H - P - (v / mx) * (H - 2 * P)
            pts.append((x, y, v))
        return pts

    pts_ok = line_points('ok')
    pts_merged = line_points('merged')
    pts_epg = line_points('epg')

    def poly(pts): return " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in pts)
    def area(pts):
        if not pts: return ''
        return f"M{pts[0][0]:.1f},{H-P} L" + " L".join(f"{x:.1f},{y:.1f}" for x, y, _ in pts) + f" L{pts[-1][0]:.1f},{H-P} Z"

    dots = ""
    hover_targets = ""
    for i, (x, y, v) in enumerate(pts_ok):
        date_str = history[i].get('date', '')
        dots += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#4ade80"/>'
        hover_targets += (
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="10" fill="transparent" '
            f'style="cursor:pointer" '
            f'onmouseover="showChartTip(event, \'{v} (OK)\', \'{date_str}\')" '
            f'onmouseout="hideChartTip()"/>')
    for i, (x, y, v) in enumerate(pts_merged):
        dots += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2" fill="#60a5fa"/>'
    for i, (x, y, v) in enumerate(pts_epg):
        dots += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2" fill="#facc15"/>'

    labels = ""
    for i in range(0, n, max(1, n // 6)):
        x = P + i * step
        d = history[i].get('date', '')[:10]
        labels += f'<text x="{x:.0f}" y="{H-14}" fill="var(--muted)" font-size="10" text-anchor="middle">{d}</text>'

    mx_all = max([h.get('ok', 0) for h in history] + [1])
    grid = ""
    for i in range(5):
        y = P + i * (H - 2 * P) / 4
        val = int(mx_all * (1 - i / 4))
        grid += f'<line x1="{P}" y1="{y:.0f}" x2="{W-P}" y2="{y:.0f}" stroke="var(--border)" stroke-width="1" stroke-dasharray="2,4"/>'
        grid += f'<text x="6" y="{y+3:.0f}" fill="var(--muted)" font-size="10">{val}</text>'

    return f'''<div class="section"><h2>Тренд (последние {n})</h2>
<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto">
{grid}
<path d="{area(pts_ok)}" fill="#4ade80" fill-opacity="0.10"/>
<polyline points="{poly(pts_ok)}" fill="none" stroke="#4ade80" stroke-width="2"/>
<path d="{area(pts_merged)}" fill="#60a5fa" fill-opacity="0.08"/>
<polyline points="{poly(pts_merged)}" fill="none" stroke="#60a5fa" stroke-width="1.5"/>
<path d="{area(pts_epg)}" fill="#facc15" fill-opacity="0.08"/>
<polyline points="{poly(pts_epg)}" fill="none" stroke="#facc15" stroke-width="1.5"/>
{dots}{hover_targets}{labels}
</svg>
<div style="margin-top:12px;font-size:12px;color:var(--muted)">
<span style="color:#4ade80">■</span> Рабочих &nbsp;
<span style="color:#60a5fa">■</span> В merged &nbsp;
<span style="color:#facc15">■</span> С EPG
</div></div>'''


def render_report(path, stats, merged, total, ok, filt, dead, unstable,
                  dur, ua, history, groups_all, logo_stats, unstable_channels,
                  quality_stats, ffprobe_stats, github_repo):
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

    ffprobe_block = ''
    if ffprobe_stats and any(ffprobe_stats.values()):
        rows = "\n".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in ffprobe_stats.items())
        ffprobe_block = (f"<div class='section'><h2>ffprobe</h2>"
                         f"<table><thead><tr><th>Статус</th><th>Каналов</th></tr></thead>"
                         f"<tbody>{rows}</tbody></table></div>")

    unstable_block = ''
    if unstable_channels:
        unstable_channels.sort(key=lambda x: x[2])
        rows = "\n".join(
            f"<tr><td>{html.escape(nm)}</td><td>{pct}%</td><td>{tot}</td></tr>"
            for nm, u, pct, tot in unstable_channels[:30])
        unstable_block = (f"<div class='section'><h2>Нестабильные</h2>"
                          f"<table><thead><tr><th>Канал</th><th>Аптайм</th><th>Проверок</th></tr></thead>"
                          f"<tbody>{rows}</tbody></table></div>")

    logo_block = ''
    if logo_stats:
        total_logo = sum(logo_stats.values()) or 1
        labels = {'from_src': 'Из источника', 'from_id': 'По iptv-org id',
                  'from_name': 'По имени', 'from_translit': 'По транслиту',
                  'from_epg_icon': 'Из EPG icon', 'from_prefix': 'По префиксу',
                  'from_txt': 'Из .txt', 'none': 'Не найдено'}
        rows = "\n".join(
            f"<tr><td>{labels.get(k, k)}</td><td>{v}</td>"
            f"<td>{v/total_logo*100:.1f}%</td></tr>"
            for k, v in sorted(logo_stats.items(), key=lambda x: -x[1]))
        logo_block = (f"<div class='section'><h2>Откуда логотипы</h2>"
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
        slow_block = (f"<div class='section'><h2>Медленные</h2>"
                      f"<table><thead><tr><th>Канал</th><th>Отклик</th></tr></thead>"
                      f"<tbody>{rows}</tbody></table></div>")

    hist_block = render_history_svg(history)
    donut_block = render_donut_svg(groups_all)
    quality_block = render_donut_svg(
        {k: v for k, v in quality_stats.items() if v > 0},
        colors_list=QUALITY_COLORS,
        title='Распределение по качеству',
        unit='КАНАЛОВ')

    denom = (ok + dead + unstable) or 1
    hpct = (ok / denom) * 100
    hb = health_bar(hpct)
    top_right = TOP_RIGHT_WIDGET.format(github_repo=github_repo,
                                        weather_html=WEATHER_HTML)
    out = fmt(
        REPORT_T,
        common_css=COMMON_CSS, top_right=top_right, theme_js=THEME_JS,
        date=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        n_playlists=len(stats), total=total, ok=ok, filtered=filt, dead=dead,
        unstable=unstable, merged=merged,
        health_block=hb, history_block=hist_block, donut_block=donut_block,
        quality_block=quality_block,
        unstable_block=unstable_block, logo_block=logo_block,
        ua_block=ua_block, slow_block=slow_block, ffprobe_block=ffprobe_block,
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


def render_index(path, url, merged, ok, total, unstable,
                 all_splits, pages_url, history):
    gh_repo = _github_repo_from_pages(pages_url)
    trend_block = render_trend_svg(history)

    weather_block, weather_kind = render_weather_card()
    currency_block = render_currency_card()
    news_block = render_news_card()
    moon_icon, moon_name = moon_phase_v2()
    quote, quote_author = quote_of_day()
    days_ny = days_to_new_year()
    svg_art = generate_svg_art()

    now = datetime.datetime.now()
    today = now.date()
    weekday_map = ['Понедельник', 'Вторник', 'Среда', 'Четверг',
                   'Пятница', 'Суббота', 'Воскресенье']
    weekday = weekday_map[now.weekday()]
    month_name = MONTHS_RU_NOM[now.month - 1]
    year_num = now.year
    months_gen = ['января','февраля','марта','апреля','мая','июня','июля',
                  'августа','сентября','октября','ноября','декабря']
    date_short = f"{now.day} {months_gen[now.month-1]}"
    calendar_block = calendar_html(today)
    holiday = HOLIDAYS_RU.get((today.month, today.day))
    holiday_plaque = f'<div class="holiday-plaque">{holiday}</div>' if holiday else ''

    encrypted_blob = ''
    if HAS_CRYPTO:
        password = generate_password(16)
        base_url = f"{pages_url.rstrip('/')}/" if pages_url else ''
        payload = {
            'playlist': url,
            'base_url': base_url,
            'splits': [
                {'name': s['name'], 'count': s['count'], 'file': s['file']}
                for s in all_splits
            ],
        }
        try:
            encrypted_blob = encrypt_payload(payload, password)
            log.info("🔐 Зашифровано: ссылка + %d сплитов", len(all_splits))
        except Exception as e:
            log.error("Шифрование: %s", e)
        if password and CFG.tg_token and CFG.tg_chat:
            send_password_to_telegram(password, url)
    else:
        log.warning("⚠️  cryptography не установлен")

    top_right = TOP_RIGHT_WIDGET.format(github_repo=gh_repo,
                                        weather_html=WEATHER_HTML)

    secure_block = f'''
    <div class="secure-block" id="secureBlock" data-enc="{encrypted_blob}">
      <div class="secure-locked" id="secureLocked">
        <div class="secure-icon">🔒</div>
        <div class="secure-title">Ссылки на плейлисты зашифрованы</div>
        <div class="secure-hint">Пароль приходит в Telegram от бота</div>
        <div class="secure-input-wrap">
          <input type="password" id="pwdInput" placeholder="Введите пароль" autocomplete="off" spellcheck="false">
          <button class="secure-btn" id="pwdBtn">Открыть</button>
        </div>
        <div class="secure-error" id="pwdError"></div>
      </div>
      <div class="secure-unlocked" id="secureUnlocked" style="display:none">
        <div class="secure-timer">⏱ Видно ещё <b id="timerVal">5:00</b></div>
        <div class="secure-main">
          <div class="secure-qr" id="qrWrap"></div>
          <div class="secure-links">
            <div class="secure-label">Основная ссылка:</div>
            <div class="link-line">
              <code id="playerUrlLocked">—</code>
              <button class="copy-btn" data-target="playerUrlLocked">📋</button>
            </div>
          </div>
        </div>
        <div class="splits-list" id="splitsWrap"></div>
        <button class="secure-btn-close" id="closeBtn">🔒 Скрыть</button>
      </div>
    </div>
    '''

    out = fmt(
        INDEX_T_SECURE,
        common_css=COMMON_CSS, top_right=top_right, theme_js=THEME_JS,
        holiday_plaque=holiday_plaque,
        svg_art=svg_art,
        weekday=weekday, year_num=year_num,
        date_short=date_short, month_name=month_name,
        weather_block=weather_block,
        weather_kind=weather_kind,
        calendar_block=calendar_block,
        moon_icon=moon_icon, moon_name=moon_name,
        currency_block=currency_block,
        days_ny=days_ny,
        quote=quote, quote_author=quote_author,
        news_block=news_block,
        trend_block=trend_block,
        secure_block=secure_block,
        merged_count=merged, ok=ok, total=total, unstable=unstable,
        github_repo=gh_repo)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(out)


def render_channels(path, channels, github_repo):
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
        q = get_quality_for_url(url, name)
        groups[group] = groups.get(group, 0) + 1
        ch_list.append({'name': name, 'group': group, 'logo': logo,
                        'url': url, 'tvg_id': tid,
                        'uptime_pct': pct, 'uptime_samples': total,
                        'quality': q})
    group_opts = "\n".join(
        f'<option value="{html.escape(g)}">{html.escape(g)} ({c})</option>'
        for g, c in sorted(groups.items()))
    top_right = TOP_RIGHT_WIDGET.format(github_repo=github_repo,
                                        weather_html=WEATHER_HTML)
    out = fmt(
        CHANNELS_T,
        common_css=COMMON_CSS, top_right=top_right, theme_js=THEME_JS,
        total=len(ch_list), epg=epg_count, group_options=group_opts,
        channels_json=json.dumps(ch_list, ensure_ascii=False))
    with open(path, 'w', encoding='utf-8') as f:
        f.write(out)


def render_rejected(path, github_repo):
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
    top_right = TOP_RIGHT_WIDGET.format(github_repo=github_repo,
                                        weather_html=WEATHER_HTML)
    out = fmt(
        REJECTED_T,
        common_css=COMMON_CSS, top_right=top_right, theme_js=THEME_JS,
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
              source_results, is_weekly, logo_stats, epg_count, quality_stats,
              ffprobe_stats):
    lines = ["<b>M3U Check v22</b>"]
    if is_weekly:
        lines.append("🗓 <i>Еженедельный отчёт</i>")
    lines.extend([
        f"Проверено: <b>{total}</b>", f"Рабочих: <b>{ok}</b>",
        f"Нестабильных: <b>{unstable}</b>", f"Отфильтровано: <b>{filt}</b>",
        f"В merged: <b>{merged}</b>", f"С EPG: <b>{epg_count}</b>",
        f"Время: {int(dur)}с", ""])
    for st in stats:
        pct = (st['ok'] / st['total'] * 100) if st['total'] else 0
        lines.append(f"• {html.escape(st['label'])}: {st['ok']}/{st['total']} ({pct:.0f}%)")
    if WHITELIST_STATS['kept'] or WHITELIST_STATS['skipped']:
        lines.append("")
        lines.append(f"<b>⭐ Whitelist:</b> {WHITELIST_STATS['kept']} каналов")
    if index_url:
        lines.append("")
        lines.append(f'<a href="{index_url}">Открыть</a>')
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
    p.add_argument('--no-hls-quality', action='store_true')
    p.add_argument('--ffprobe', action='store_true')
    p.add_argument('--ffprobe-workers', type=int, default=4)
    p.add_argument('--ffprobe-timeout', type=int, default=8)
    p.add_argument('--ffprobe-cache-days', type=int, default=3)
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
    global CFG, CATEGORIES, FILTERS, UPTIME_NEW, REJECTED, QUALITY_MAP_NEW
    global FFPROBE_MAP_NEW, FFPROBE_SEM, WEATHER_HTML
    CFG = parse_args()
    CFG.verify_ssl = not CFG.no_ssl_verify
    if CFG.no_ssl_verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    setup_logging(CFG.log, CFG.quiet)

    REJECTED = []
    QUALITY_MAP_NEW = {}
    FFPROBE_MAP_NEW = {}
    FFPROBE_SEM = threading.Semaphore(max(1, CFG.ffprobe_workers))

    if CFG.ffprobe:
        if HAS_FFPROBE:
            emit("ffprobe: включён (UA=wink)")
        else:
            emit("ffprobe: НЕ НАЙДЕН")
            CFG.ffprobe = False

    emit("Загружаю погоду...")
    WEATHER_HTML = fetch_weather()
    emit(f"  → {WEATHER_HTML}")

    CATEGORIES = load_json(CFG.categories)
    FILTERS = load_json(CFG.filters)
    FILTERS['_name_block'] = compile_patterns(FILTERS.get('name_blocklist', []))
    FILTERS['_name_exclude'] = compile_patterns(FILTERS.get('name_exclude', []))
    FILTERS['_group_exclude'] = compile_patterns(FILTERS.get('group_exclude', []))
    FILTERS['_url_block'] = compile_patterns(FILTERS.get('url_blocklist', []))
    FILTERS['_suspicious'] = compile_patterns(FILTERS.get('suspicious_patterns', []))
    FILTERS['_malformed'] = compile_patterns(FILTERS.get('malformed_patterns', []))
    compile_whitelist()
    if WHITELIST_URLS or WHITELIST_NAMES:
        emit(f"⭐ Whitelist: url={len(WHITELIST_URLS)} name={len(WHITELIST_NAMES)}")
    if HAS_CRYPTO:
        emit("🔐 Шифрование: включено (AES-256-GCM)")
    else:
        emit("⚠️  cryptography не установлен")

    uptime_path = os.path.join(CFG.docs_dir, 'uptime.json')
    load_uptime(uptime_path)
    if UPTIME:
        emit(f"Аптайм: {len(UPTIME)} URL")

    quality_path = os.path.join(CFG.docs_dir, 'quality.json')
    load_quality_cache(quality_path)
    if QUALITY_MAP:
        emit(f"Качество: {len(QUALITY_MAP)} URL")

    ffprobe_path = os.path.join(CFG.docs_dir, 'ffprobe.json')
    load_ffprobe_cache(ffprobe_path)
    if FFPROBE_MAP:
        emit(f"ffprobe: {len(FFPROBE_MAP)} URL")

    emit("Загружаю базу iptv-org...")
    load_iptv_org()
    emit("Загружаю EPG iptvx.one...")
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

    removed = cleanup_old_splits(CFG.docs_dir)
    if removed:
        emit(f"🧹 Удалено старых сплитов: {removed}")

    pages = CFG.pages_url.rstrip('/')
    gh_repo = _github_repo_from_pages(pages)

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
        quality_splits = write_quality_splits(ordered_merged, CFG.docs_dir)
        all_splits = splits + quality_splits

        save_split_map({s['key']: s['file'] for s in all_splits})

        for s in splits:
            emit(f"    split: {s['name']} ({s['count']}) → {s['file']}")
        for s in quality_splits:
            emit(f"    quality: {s['name']} ({s['count']}) → {s['file']}")

        total = sum(s['total'] for s in stats_list)
        ok = sum(s['ok'] for s in stats_list)
        filt = sum(s.get('filtered', 0) for s in stats_list)
        unstable = sum(s.get('unstable', 0) for s in stats_list)
        dead = total - ok - filt - unstable
        epg_count = sum(1 for e, _ in ordered_merged if get_tvg_id(e))

        quality_stats = {'4K': 0, 'HD': 0, 'SD': 0, 'Unknown': 0}
        for e, u in ordered_merged:
            q = get_quality_for_url(u, get_name(e))
            quality_stats[q] = quality_stats.get(q, 0) + 1

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
        render_rejected(os.path.join(CFG.docs_dir, 'rejected.html'), gh_repo)
        emit(f"\nrejected.csv: {rejected_count} записей")

        render_report(os.path.join(CFG.docs_dir, 'report.html'),
                      stats_list, len(ordered_merged), total, ok, filt, dead, unstable,
                      time.time() - started, ua_totals, history, groups_all,
                      LOGO_STATS, unstable_list, quality_stats, FFPROBE_STATS,
                      gh_repo)

        purl = f'{pages}/{CFG.merged_name}' if pages else CFG.merged_name
        iurl = f'{pages}/index.html' if pages else ''
        qr_path = os.path.join(CFG.docs_dir, 'qr.png')
        if pages and HAS_QR:
            generate_qr(purl, qr_path)

        render_index(os.path.join(CFG.docs_dir, 'index.html'),
                     purl, len(ordered_merged), ok, total, unstable,
                     all_splits, pages, history)
        render_channels(os.path.join(CFG.docs_dir, 'channels.html'), ordered_merged, gh_repo)
        write_pwa_assets(CFG.docs_dir)

        save_uptime(uptime_path, all_current_urls)
        save_quality_cache(quality_path, all_current_urls)
        save_ffprobe_cache(ffprobe_path, all_current_urls)
        emit(f"\nАптайм: {len(UPTIME)} URL")
        emit(f"Качество: {len(QUALITY_MAP)} URL")
        if CFG.ffprobe:
            emit(f"ffprobe: {len(FFPROBE_MAP)} URL")
        if WHITELIST_STATS['kept']:
            emit(f"⭐ Whitelist: {WHITELIST_STATS['kept']} каналов через VIP")

        logo_total = sum(v for k, v in LOGO_STATS.items() if k != 'none')
        logo_all = sum(LOGO_STATS.values()) or 1
        emit(f"\nГотово за {int(time.time() - started)}с. "
             f"OK={ok}, фильтр={filt}, нестабильных={unstable}, "
             f"merged={len(ordered_merged)}, с EPG={epg_count}, "
             f"лого={logo_total}/{logo_all} ({logo_total/logo_all*100:.0f}%)")
        emit(f"    Качество: 4K={quality_stats['4K']}, HD={quality_stats['HD']}, "
             f"SD={quality_stats['SD']}, Unknown={quality_stats['Unknown']}")
        if CFG.ffprobe:
            emit(f"    ffprobe: ok={FFPROBE_STATS['ok']}, fail={FFPROBE_STATS['fail']}, "
                 f"cached_ok={FFPROBE_STATS['cached_ok']}, cached_fail={FFPROBE_STATS['cached_fail']}")

        if CFG.tg_token and CFG.tg_chat:
            is_sunday = datetime.datetime.now().weekday() == 6
            tg_report(stats_list, total, ok, filt, unstable, len(ordered_merged),
                      time.time() - started, iurl, source_results,
                      is_weekly=(CFG.weekly_backup and is_sunday),
                      logo_stats=LOGO_STATS, epg_count=epg_count,
                      quality_stats=quality_stats, ffprobe_stats=FFPROBE_STATS)
            if CFG.tg_send_merged and merged_path:
                tg_file(CFG.tg_token, CFG.tg_chat, merged_path, caption="Merged")
            if CFG.weekly_backup and is_sunday:
                for fn in ('report.html', 'channels.csv', 'channels.html', 'rejected.csv'):
                    fp = os.path.join(CFG.docs_dir, fn)
                    if os.path.isfile(fp):
                        tg_file(CFG.tg_token, CFG.tg_chat, fp, caption=f"Weekly: {fn}")

    finally:
        cache.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
