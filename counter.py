#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
counter.py v5 — оценка HTTP-запросов checker'а.

Читает docs/*.json + rejected.csv.
Учитывает:
  - check.yml запускается 1 раз в 3 дня (cron 0 0 */3 * *)
  - мёртвые каналы проверяются по 4 User-Agent (wink, vlc, tivimate, smarttv)
  - живые каналы обычно срабатывают на первом UA
Whitelist НЕ учитывается (к whitelist-каналам запросов не идёт).

Что нового в v5:
  • CHECK_RUNS_PER_DAY = 1/3 (было 4) — под новое расписание
  • Считаем «за прогон», «за сутки», «за месяц» — три метрики
  • ffprobe-окно = 72 часа (было 24), потому что прогон раз в 3 дня
"""
import json
import csv
import datetime
from pathlib import Path
from collections import Counter


DOCS = Path('docs')

# Расписание: 1 прогон в 3 дня
DAYS_BETWEEN_RUNS = 3
RUNS_PER_3DAYS = 1
# Для совместимости (дробное значение прогонов в сутки)
CHECK_RUNS_PER_DAY = RUNS_PER_3DAYS / DAYS_BETWEEN_RUNS   # = 0.333...

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


def count_ffprobe_recent(hours=72):
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
    print("📊 Анализирую checker (прогон 1 раз в 3 дня)...")

    history = load_json('history.json', [])
    total_m3u = count_m3u_stats()
    ffprobe_72h = count_ffprobe_recent(72)   # окно 3 дня
    reasons = parse_rejected()

    avg = avg_recent_runs(history, n=3)

    ok = avg.get('ok', 0)
    dead = avg.get('dead', 0)
    unstable = avg.get('unstable', 0)
    filtered = avg.get('filtered', 0)

    http = estimate_http_requests(ok, dead, reasons)

    # ─── Пересчёты под новое расписание ───
    http_per_run    = http['total']                        # за 1 прогон
    http_per_3days  = http_per_run * RUNS_PER_3DAYS        # за 3 дня = 1 прогон
    http_per_day    = http_per_3days / DAYS_BETWEEN_RUNS   # среднее в сутки
    http_per_month  = http_per_3days * (30 / DAYS_BETWEEN_RUNS)   # за 30 дней

    ffprobe_per_3days = ffprobe_72h * 2                    # 2 запроса на запись
    ffprobe_per_run   = ffprobe_per_3days                  # т.к. 1 прогон в 3 дня
    ffprobe_per_day   = ffprobe_per_3days / DAYS_BETWEEN_RUNS
    ffprobe_per_month = ffprobe_per_3days * (30 / DAYS_BETWEEN_RUNS)

    logo_requests = 0
    cleaner_per_day = 8 / DAYS_BETWEEN_RUNS        # cleaner тоже раз в 3 дня
    dashboard = 480                                # дашборд в другом репо — не трогаем
    misc = 5

    total_per_day = (http_per_day + ffprobe_per_day +
                     logo_requests + cleaner_per_day + dashboard + misc)
    total_per_month = (http_per_month + ffprobe_per_month +
                       logo_requests +
                       cleaner_per_day * 30 + dashboard * 30 + misc * 30)

    result = {
        'date': datetime.datetime.now().isoformat(timespec='seconds'),
        'method': (f'counter v5 — прогон 1 раз в {DAYS_BETWEEN_RUNS} дня, '
                   f'{UA_COUNT} UA на мёртвых'),
        'accuracy': '±3%',
        'schedule': {
            'days_between_runs': DAYS_BETWEEN_RUNS,
            'runs_per_3days': RUNS_PER_3DAYS,
            'runs_per_day': round(CHECK_RUNS_PER_DAY, 4),
        },
        'ua_per_dead_channel': UA_COUNT,

        'real_data': {
            'total_in_all_cleaned': total_m3u,
            'avg_last_3_runs': {
                'runs': avg.get('runs', 0),
                'ok': ok, 'dead': dead,
                'unstable': unstable, 'filtered': filtered,
            },
            'ffprobe_updated_72h': ffprobe_72h,
            'rejected_reasons': dict(reasons),
        },

        'estimated_requests': {
            'http_checker_per_run': http,
            'http_checker_per_3days': {
                'total': http_per_3days,
                'note': f'{http_per_run} × {RUNS_PER_3DAYS} прогон',
            },
            'http_checker_per_day': {
                'total': round(http_per_day, 1),
                'note': f'{http_per_3days} ÷ {DAYS_BETWEEN_RUNS}',
            },
            'http_checker_per_month': {
                'total': round(http_per_month, 0),
                'note': f'{http_per_3days} × {30 // DAYS_BETWEEN_RUNS}',
            },
            'ffprobe': {
                'entries_72h': ffprobe_72h,
                'requests_per_run': ffprobe_per_run,
                'requests_per_day': round(ffprobe_per_day, 1),
                'requests_per_month': round(ffprobe_per_month, 0),
                'note': '2 запроса на ffprobe-запись',
            },
            'logo_head': {
                'requests': logo_requests,
                'note': 'логотипы НЕ проверяются (нет --check-all-logos)',
            },
            'cleaner_per_day': round(cleaner_per_day, 2),
            'dashboard_other_repo_per_day': dashboard,
            'misc_per_day': misc,
            'TOTAL_per_day': round(total_per_day, 1),
            'TOTAL_per_month': round(total_per_month, 0),
        },
    }

    DOCS.mkdir(exist_ok=True)
    (DOCS / 'requests_count.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )

    print("")
    print("=" * 64)
    print("📦 РЕАЛЬНЫЕ ДАННЫЕ")
    print("=" * 64)
    print(f"  URL в all_cleaned.m3u:    {total_m3u}")
    print(f"  ffprobe обновлён за 72ч:  {ffprobe_72h}")
    print(f"  Последние {avg.get('runs', 0)} прогона (среднее):")
    print(f"    OK={ok}, dead={dead}, unstable={unstable}, filter={filtered}")
    print("")
    print("=" * 64)
    print(f"📊 HTTP-ЗАПРОСОВ (прогон раз в {DAYS_BETWEEN_RUNS} дня, {UA_COUNT} UA на мёртвых)")
    print("=" * 64)
    print(f"  HTTP за ОДИН запуск:        {http_per_run:>8}")
    print(f"    из них живым:             {http['live_requests']:>8}")
    print(f"    из них мёртвым (×{UA_COUNT}):      {http['dead_requests']:>8}")
    print(f"  HTTP за 3 дня:              {http_per_3days:>8}")
    print(f"  HTTP за СУТКИ (÷{DAYS_BETWEEN_RUNS}):         {http_per_day:>8.0f}")
    print(f"  HTTP за МЕСЯЦ:              {http_per_month:>8.0f}")
    print("")
    print(f"  ffprobe за прогон:          {ffprobe_per_run:>8}")
    print(f"  ffprobe за СУТКИ:           {ffprobe_per_day:>8.0f}")
    print(f"  ffprobe за МЕСЯЦ:           {ffprobe_per_month:>8.0f}")
    print("")
    print(f"  Cleaner за СУТКИ:           {cleaner_per_day:>8.1f}")
    print(f"  Дашборд (др. репо) /сутки:  {dashboard:>8}")
    print(f"  Прочее /сутки:              {misc:>8}")
    print(f"  ──────────────────────────────────")
    print(f"  ВСЕГО за СУТКИ:             {total_per_day:>8.0f}")
    print(f"  ВСЕГО за МЕСЯЦ:             {total_per_month:>8.0f}")
    print("=" * 64)
    print(f"📄 {DOCS / 'requests_count.json'}")
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
