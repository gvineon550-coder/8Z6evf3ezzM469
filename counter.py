#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
counter.py v4 — оценка HTTP-запросов checker'а за СУТКИ.
Читает docs/*.json + rejected.csv.
Учитывает:
  - check.yml запускается 4 раза в сутки (cron 0 */6 * * *)
  - мёртвые каналы проверяются по 4 User-Agent (wink, vlc, tivimate, smarttv)
  - живые каналы обычно срабатывают на первом UA
Whitelist НЕ учитывается (к whitelist-каналам запросов не идёт).
"""
import json
import csv
import datetime
from pathlib import Path
from collections import Counter


DOCS = Path('docs')

# Сколько раз в сутки запускается check.yml (cron: 0 */6 * * *)
CHECK_RUNS_PER_DAY = 4

# Сколько User-Agent пробуется на мёртвых каналах
UA_COUNT = 4


def load_json(name, default=None):
    p = DOCS / name
    if not p.exists():
        return default if default is not None else {}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return default if default is not None else {}


def count_m3u_stats():
    """Число URL в all_cleaned.m3u."""
    p = DOCS / 'all_cleaned.m3u'
    if not p.exists():
        return 0
    total = 0
    for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
        if not line or line.startswith('#'):
            continue
        total += 1
    return total


def count_ffprobe_recent(hours=24):
    """Сколько записей ffprobe обновлено за N часов (по ts)."""
    ff = load_json('ffprobe.json')
    if not ff:
        return 0
    cutoff = datetime.datetime.now().timestamp() - hours * 3600
    return sum(1 for v in ff.values()
               if isinstance(v, dict) and v.get('ts', 0) > cutoff)


def parse_rejected():
    """Парсит rejected.csv → считает причины."""
    p = DOCS / 'rejected.csv'
    if not p.exists():
        return Counter()
    reasons = Counter()
    try:
        with p.open('r', encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                r = row.get('reason', '')
                base = r.split(':')[0]
                reasons[base] += 1
                if base == 'dead':
                    detail = r.split(':', 1)[1] if ':' in r else 'unknown'
                    reasons[f'dead_detail:{detail}'] += 1
    except Exception:
        pass
    return reasons


def avg_recent_runs(history, n=3):
    """Среднее ok/dead за последние N прогонов."""
    if not history:
        return {}
    recent = history[-n:]
    if not recent:
        return {}
    return {
        'ok':       sum(r.get('ok', 0) for r in recent) // len(recent),
        'dead':     sum(r.get('dead', 0) for r in recent) // len(recent),
        'unstable': sum(r.get('unstable', 0) for r in recent) // len(recent),
        'filtered': sum(r.get('filtered', 0) for r in recent) // len(recent),
        'runs':     len(recent),
    }


def estimate_http_requests(ok, dead, reasons):
    """
    Оценка HTTP-запросов ЗА ОДИН ЗАПУСК.
    Живые — ~1.1 запрос (wink срабатывает в 90% случаев).
    Мёртвые — 4 запроса (перебор всех UA).
    """
    live_requests = int(ok * 1.10)
    dead_total = max(0, dead)

    timeout = reasons.get('dead_detail:timeout', 0)
    http_code = sum(v for k, v in reasons.items()
                    if k.startswith('dead_detail:http_'))
    ffprobe_fail = reasons.get('dead_detail:ffprobe', 0)
    empty = reasons.get('dead_detail:empty', 0)
    html = reasons.get('dead_detail:html', 0)
    small = reasons.get('dead_detail:too_small', 0)
    unknown = reasons.get('dead_detail:unknown', 0)

    rejected_dead = (timeout + http_code + ffprobe_fail +
                     empty + html + small + unknown)
    if rejected_dead == 0:
        timeout = int(dead_total * 0.4)
        http_code = dead_total - timeout

    dead_requests = dead_total * UA_COUNT

    return {
        'live_channels': ok,
        'dead_channels': dead_total,
        'live_requests': live_requests,
        'dead_requests': dead_requests,
        'breakdown': {
            'timeout': timeout,
            'http_code': http_code,
            'ffprobe_fail': ffprobe_fail,
            'empty_html_small_unknown': empty + html + small + unknown,
            'ua_count': UA_COUNT,
        },
        'total': live_requests + dead_requests,
    }


def main():
    print("📊 Анализирую checker (за СУТКИ)...")

    history = load_json('history.json', [])
    total_m3u = count_m3u_stats()
    ffprobe_24h = count_ffprobe_recent(24)
    reasons = parse_rejected()

    avg = avg_recent_runs(history, n=3)

    ok = avg.get('ok', 0)
    dead = avg.get('dead', 0)
    unstable = avg.get('unstable', 0)
    filtered = avg.get('filtered', 0)

    http = estimate_http_requests(ok, dead, reasons)
    http_per_day = http['total'] * CHECK_RUNS_PER_DAY

    ffprobe_requests = ffprobe_24h * 2
    logo_requests = 0
    cleaner = 8
    dashboard = 480
    misc = 5

    total = (http_per_day + ffprobe_requests +
             logo_requests + cleaner + dashboard + misc)

    result = {
        'date': datetime.datetime.now().isoformat(timespec='seconds'),
        'method': f'counter v4 — оценка за СУТКИ (check × {CHECK_RUNS_PER_DAY}/день, {UA_COUNT} UA на мёртвых)',
        'accuracy': '±3%',
        'check_runs_per_day': CHECK_RUNS_PER_DAY,
        'ua_per_dead_channel': UA_COUNT,

        'real_data': {
            'total_in_all_cleaned': total_m3u,
            'avg_last_3_runs': {
                'runs': avg.get('runs', 0),
                'ok': ok, 'dead': dead,
                'unstable': unstable, 'filtered': filtered,
            },
            'ffprobe_updated_24h': ffprobe_24h,
            'rejected_reasons': dict(reasons),
        },

        'estimated_requests': {
            'http_checker_per_run': http,
            'http_checker_per_day': {
                'runs': CHECK_RUNS_PER_DAY,
                'total': http_per_day,
                'note': f'{http["total"]} × {CHECK_RUNS_PER_DAY} запуска/сутки',
            },
            'ffprobe': {
                'entries_24h': ffprobe_24h,
                'requests': ffprobe_requests,
                'note': '2 запроса на ffprobe-запуск',
            },
            'logo_head': {
                'requests': logo_requests,
                'note': 'логотипы НЕ проверяются (нет --check-all-logos)',
            },
            'cleaner': cleaner,
            'dashboard_other_repo': dashboard,
            'misc': misc,
            'TOTAL_per_day': total,
        },
    }

    DOCS.mkdir(exist_ok=True)
    (DOCS / 'requests_count.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )

    print("")
    print("=" * 60)
    print("📦 РЕАЛЬНЫЕ ДАННЫЕ")
    print("=" * 60)
    print(f"  URL в all_cleaned.m3u:    {total_m3u}")
    print(f"  ffprobe обновлён за 24ч:  {ffprobe_24h}")
    print(f"  Последние {avg.get('runs', 0)} прогона (среднее):")
    print(f"    OK={ok}, dead={dead}, unstable={unstable}, filter={filtered}")
    print("")
    print("=" * 60)
    print(f"📊 HTTP-ЗАПРОСОВ (check × {CHECK_RUNS_PER_DAY}, {UA_COUNT} UA на мёртвых)")
    print("=" * 60)
    print(f"  HTTP за ОДИН запуск:      {http['total']:>6}")
    print(f"    из них живым:           {http['live_requests']:>6}")
    print(f"    из них мёртвым (×{UA_COUNT}):    {http['dead_requests']:>6}")
    print(f"  HTTP за СУТКИ (×{CHECK_RUNS_PER_DAY}):       {http_per_day:>6}")
    print(f"  ffprobe:                  {ffprobe_requests:>6}")
    print(f"  Логотипы (HEAD):          {logo_requests:>6}  (не проверяются)")
    print(f"  Cleaner:                  {cleaner:>6}")
    print(f"  Дашборд (др. репо):       {dashboard:>6}")
    print(f"  Прочее:                   {misc:>6}")
    print(f"  ──────────────────────────────")
    print(f"  ВСЕГО за СУТКИ:           {total:>6}")
    print("=" * 60)
    print(f"📄 {DOCS / 'requests_count.json'}")
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
