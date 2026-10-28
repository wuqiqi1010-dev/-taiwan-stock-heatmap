"""Archive fetched official quotes as unverified observation snapshots.

IMPORTANT: These are fetch-date observations, NOT validated trading-day bars.
Never calculate 5/20/60 trading-day returns from this archive until the
underlying exchange trading date has been independently verified.
"""
import datetime as dt
import hashlib
import json
import pathlib
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'data.json'
DEST = ROOT / 'history'
INDEX = DEST / 'index.json'


def main():
    data = json.loads(SOURCE.read_text(encoding='utf-8'))
    if not isinstance(data.get('records'), list) or len(data['records']) < 500:
        raise SystemExit('Refusing to archive incomplete quote data')
    if not all(data.get('sources', {}).get(m, {}).get('ok') for m in ('TWSE', 'TPEx')):
        raise SystemExit('Both exchange feeds must succeed')
    fetched = dt.datetime.fromisoformat(data['fetched_at'])
    local = fetched.astimezone(ZoneInfo('Asia/Taipei'))
    if local.hour < 16:
        raise SystemExit('Not archiving before 16:00 Asia/Taipei')
    if local.weekday() >= 5:
        print('Weekend: no new archive')
        return
    if data.get('date_verified') is not True:
        verified_market_date = None
    else:
        verified_market_date = data.get('market_date')
    records = sorted(data['records'], key=lambda r: (r.get('market',''), r.get('code','')))
    digest = hashlib.sha256(json.dumps(records, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    DEST.mkdir(exist_ok=True)
    day = local.date().isoformat()
    path = DEST / (day + '.json')
    if path.exists():
        old = json.loads(path.read_text(encoding='utf-8'))
        if old.get('quotes_sha256') == digest:
            print('Unchanged archive:', day)
            return
    # Archive one observation per local calendar date; do not mislabel as trading date.
    archive = {
        'observation_date': day,
        'fetched_at': data['fetched_at'],
        'verified_market_date': verified_market_date,
        'eligible_for_trading_day_returns': bool(verified_market_date),
        'quotes_sha256': digest,
        'record_count': len(records),
        'records': records,
    }
    path.write_text(json.dumps(archive, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    paths = sorted(p.name for p in DEST.glob('????-??-??.json'))
    INDEX.write_text(json.dumps({
        'note': 'Observation-date archives; NOT validated trading sessions unless verified_market_date is populated.',
        'snapshots': paths,
        'verified_trading_sessions': sum(bool(json.loads((DEST / p).read_text(encoding='utf-8')).get('verified_market_date')) for p in paths),
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Archived', path, 'verified trading date:', verified_market_date)


if __name__ == '__main__':
    main()
