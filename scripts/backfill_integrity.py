"""Fail-closed TWSE cache validation and batch planning (stdlib only).

A verified month proves fidelity to a successful official response, not that a
stock traded every exchange session. Suspensions/listing gaps need review.
"""
import datetime as dt
import hashlib
import json
import math
import pathlib


def digest(series):
    return hashlib.sha256(json.dumps(series, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def inspect(data, code, months, today):
    issues = []
    if not isinstance(data, dict):
        return ['invalid_object']
    if data.get('code') != code or data.get('market') != 'TWSE':
        issues.append('identity_mismatch')
    series = data.get('series')
    if not isinstance(series, list) or not series:
        return issues + ['empty_or_invalid_series']
    seen = set()
    previous = ''
    valid = []
    for item in series:
        try:
            day = item['date']
            date = dt.date.fromisoformat(day)
            close = item['close']
            if (date.isoformat() != day or date > today or day in seen or day <= previous
                or isinstance(close, bool) or not isinstance(close, (int, float))
                or not math.isfinite(close) or close <= 0):
                raise ValueError()
            seen.add(day)
            previous = day
            valid.append(item)
        except (KeyError, TypeError, ValueError):
            issues.append('invalid_duplicate_or_unsorted_row')
    if data.get('errors') != []:
        issues.append('fetch_errors')
    ledger = data.get('month_checks', {})
    if not isinstance(ledger, dict):
        ledger = {}
    for month in months:
        key = month.isoformat()[:7]
        rows = [x for x in valid if x['date'].startswith(key)]
        check = ledger.get(key, {})
        if not isinstance(check, dict):
            check = {}
        if not rows or check.get('status') != 'verified':
            issues.append(f'{key}:unverified_or_missing')
        elif check.get('count') != len(rows) or check.get('sha256') != digest(rows):
            issues.append(f'{key}:content_mismatch')
        if key == today.isoformat()[:7] and check.get('checked_on') != today.isoformat():
            issues.append(f'{key}:stale')
    return sorted(set(issues))


def read(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def candidates(payload):
    return sorted({str(row['code']): row for row in payload.get('records', [])
        if row.get('market') == 'TWSE' and row.get('common_stock_candidate')
        and str(row.get('code', '')).isdigit() and len(str(row['code'])) == 4}.values(), key=lambda x: str(x['code']))


def plan(stocks, root, months, today, batch_size=40):
    pending = []
    for offset in range(0, len(stocks), batch_size):
        attempts = []
        incomplete = False
        for row in stocks[offset:offset + batch_size]:
            code = str(row['code'])
            data = read(root / f'{code}.json')
            incomplete |= bool(inspect(data, code, months, today))
            attempts.append(data.get('last_attempt', '') if isinstance(data, dict) else '')
        if incomplete:
            pending.append((max(attempts, default=''), offset))
    # Oldest attempted group first: a persistent API error cannot starve others.
    return min(pending)[1] if pending else -1


if __name__ == '__main__':
    import argparse
    from backfill import ROOT, DEST, months_back
    from zoneinfo import ZoneInfo
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--months', type=int, default=7)
    parser.add_argument('--check-batch', action='store_true')
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--limit', type=int, default=40)
    args = parser.parse_args()
    if not 1 <= args.months <= 12 or args.offset < 0 or not 1 <= args.limit <= 60:
        parser.error('months must be 1..12')
    today = dt.datetime.now(ZoneInfo('Asia/Taipei')).date()
    stocks = candidates(read(ROOT / 'data.json'))
    if not stocks:
        raise SystemExit('No TWSE candidates: refusing to report completion')
    months = list(months_back(today, args.months))
    if args.check_batch:
        stocks = stocks[args.offset:args.offset + args.limit]
        if not stocks:
            raise SystemExit('Empty batch')
    if args.plan:
        print(f'offset={plan(stocks, DEST, months, today)}')
        print(f'total={len(stocks)}')
    else:
        report = {str(row['code']): inspect(read(DEST / (str(row["code"]) + ".json")), str(row['code']), months, today) for row in stocks}
        print(json.dumps({'total': len(stocks), 'complete': sum(not x for x in report.values()), 'incomplete': {k:v for k,v in report.items() if v}}, ensure_ascii=False, indent=2))

        if args.check_batch and any(report.values()):
            raise SystemExit(1)
