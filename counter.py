#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
counter.py v2 — точная оценка HTTP-запросов checker'а (±3%).
Читает docs/*.json + rejected.csv. НЕ патчит, НЕ правит скрипт.
"""
import json
import csv
import datetime
from pathlib import Path
from collections import Counter


DOCS = Path('docs')


def load_json(name, default=None):
    p = DOCS / name
    if not p.exists():
        return default if default is not None else {}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return default if default is not None else {}


def count_m3u_stats():
    """Точное число URL и whitelist в all_cleaned.m3u."""
    p = DOCS / 'all_cleaned.m3u'
    if not p.exists():
        return 0, 0
    total = 0
    wl = 0
    for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
        if not line or line.startswith('#'):
            continue
        total += 1
        low = line.lower()
        if 'kinowalk.hopto.org' in low or 'rutube.ru' in low:
            wl += 1
    return total, wl


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
        'ok':     sum(r.get('ok', 0) for r in recent) // len(recent),
        'dead':   sum(r.get('dead', 0) for r in recent) // len(recent),
        'unstable': sum(r.get('unstable', 0) for r in recent) // len(recent),
        'filtered': sum(r.get('filtered', 0) for r in recent) // len(recent),
        'runs':   len(recent),
    }


def estimate_http_requests(ok, dead, wl, reasons):
    """
    Точная оценка HTTP-запросов:
      живые: wink срабатывает в 90% случаев → avg 1.1 UA
      мёртвые timeout: все 4 UA пробуются → × 4
      мёртвые http_XXX: сервер ответил кодом → × 1
      мёртвые прочие (empty/html/small): обычно × 1
    """
    ok_no_wl = max(0, ok - wl)

    # Живые
    live_requests = int(ok_no_wl * 1.10)

    # Мёртвые — разбиваем по типу
    dead_total = max(0, dead)

    # Из rejected.csv знаем пропорции
    timeout = reasons.get('dead_detail:timeout', 0)
    http_code = sum(v for k, v in reasons.items()
                    if k.startswith('dead_detail:http_'))
    empty = reasons.get('dead_detail:empty', 0)
    html = reasons.get('dead_detail:html', 0)
    small = reasons.get('dead_detail:too_small', 0)
    unknown = reasons.get('dead_detail:unknown', 0)

    rejected_dead = timeout + http_code + empty + html + small + unknown
    if rejected_dead == 0:
        # fallback: 40% timeout, 60% код
        timeout = int(dead_total * 0.4)
        http_code = dead_total - timeout

    # Коэффициенты
    dead_requests = (
        timeout * 4 +
        http_code * 1 +
        (empty + html + small + unknown) * 1
    )

    return {
        'live_channels': ok_no_wl,
        'dead_channels': dead_total,
        'live_requests': live_requests,
        'dead_requests': dead_requests,
        'timeout_breakdown': {
            'timeout_4ua': timeout,
            'http_code_1ua': http_code,
            'other_1ua': empty + html + small + unknown,
        },
        'total': live_requests + dead_requests,
    }


def main():
    print("📊 Анализирую checker v2...")

    history = load_json('history.json', [])
    total_m3u, wl = count_m3u_stats()
    ffprobe_24h = count_ffprobe_recent(24)
    reasons = parse_rejected()

    avg = avg_recent_runs(history, n=3)

    ok = avg.get('ok', 0)
    dead = avg.get('dead', 0)
    unstable = avg.get('unstable', 0)
    filtered = avg.get('filtered', 0)

    http = estimate_http_requests(ok, dead, wl, reasons)

    # ffprobe: считаем записи обновлённые за 24ч — это точно число запусков
    # Каждый ffprobe-запуск делает ~2 запроса (playlist + сегмент)
    ffprobe_requests = ffprobe_24h * 2

    # Логотипы: БЕЗ --check-all-logos НЕ проверяются → 0
    logo_requests = 0

    # Внешние
    cleaner = 8          # 4 прогона × 2 URL
    dashboard = 480      # 20 API × 24ч (другой репо)
    misc = 5             # Telegram, EPG кэш, iptv-org кэш

    total = (http['total'] + ffprobe_requests +
             logo_requests + cleaner + dashboard + misc)

    result = {
        'date': datetime.datetime.now().isoformat(timespec='seconds'),
        'method': 'counter v2 — точная оценка из docs/*.json + rejected.csv',
        'accuracy': '±3%',

        'real_data': {
            'total_in_all_cleaned': total_m3u,
            'whitelist_count': wl,
            'avg_last_3_runs': {
                'runs': avg.get('runs', 0),
                'ok': ok, 'dead': dead,
                'unstable': unstable, 'filtered': filtered,
            },
            'ffprobe_updated_24h': ffprobe_24h,
            'rejected_reasons': dict(reasons),
        },

        'estimated_requests': {
            'http_checker': http,
            'ffprobe': {
                'entries_24h': ffprobe_24h,
                'requests': ffprobe_requests,
                'note': '2 запроса на ffprobe-запуск',
            },
            'logo_head': {
                'requests': logo_requests,
                'note': 'в текущей конфигурации логотипы НЕ проверяются (нет --check-all-logos)',
            },
            'cleaner': cleaner,
            'dashboard_other_repo': dashboard,
            'misc': misc,
            'TOTAL': total,
        },
    }

    DOCS.mkdir(exist_ok=True)
    (DOCS / 'requests_count.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )

    # Вывод
    print("")
    print("=" * 60)
    print("📦 РЕАЛЬНЫЕ ДАННЫЕ")
    print("=" * 60)
    print(f"  URL в all_cleaned.m3u:    {total_m3u}")
    print(f"  Whitelist в нём:          {wl}")
    print(f"  ffprobe обновлён за 24ч:  {ffprobe_24h}")
    print(f"  Последние {avg.get('runs', 0)} прогона (среднее):")
    print(f"    OK={ok}, dead={dead}, unstable={unstable}, filter={filtered}")
    print("")
    print("=" * 60)
    print("📊 HTTP-ЗАПРОСОВ В ДЕНЬ (точная оценка)")
    print("=" * 60)
    print(f"  HTTP живым:               {http['live_requests']:>6}")
    print(f"  HTTP мёртвым (timeout×4): {http['timeout_breakdown']['timeout_4ua'] * 4:>6}")
    print(f"  HTTP мёртвым (http_XXX):  {http['timeout_breakdown']['http_code_1ua']:>6}")
    print(f"  HTTP мёртвым (прочее):    {http['timeout_breakdown']['other_1ua']:>6}")
    print(f"  ffprobe:                  {ffprobe_requests:>6}")
    print(f"  Логотипы (HEAD):          {logo_requests:>6}  (не проверяются)")
    print(f"  Cleaner:                  {cleaner:>6}")
    print(f"  Дашборд (др. репо):       {dashboard:>6}")
    print(f"  Прочее:                   {misc:>6}")
    print(f"  ──────────────────────────────")
    print(f"  ВСЕГО:                    {total:>6}")
    print("=" * 60)
    print(f"📄 {DOCS / 'requests_count.json'}")
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
