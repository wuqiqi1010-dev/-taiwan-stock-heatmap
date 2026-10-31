
"""V6.1A: batched TWSE historical backfill.

Historical files are separate from the live heatmap.
Uses TWSE official monthly STOCK_DAY data.
"""
import argparse
import datetime as dt
import http.client
import json
import math
import os
import tempfile
from backfill_integrity import candidates as select_candidates, digest, has_reached_first_weekday, inspect, read
import pathlib
import socket
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data.json"
DEST = ROOT / "historical" / "twse"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Accept-Encoding": "identity",
    "Connection": "close",
}


def fetch(url):
    last_error = None

    for attempt in range(1, 5):
        try:
            request = urllib.request.Request(
                url, headers=HEADERS
            )

            with urllib.request.urlopen(
                request, timeout=45
            ) as response:
                raw = response.read()

            return json.loads(
                raw.decode("utf-8-sig")
            )

        except (
            http.client.IncompleteRead,
            urllib.error.URLError,
            socket.timeout,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
        ) as error:
            last_error = error
            print(
                f"Retry {attempt}/4: {error}",
                flush=True,
            )
            time.sleep(attempt * 3)

    raise RuntimeError(str(last_error))


def months_back(today, count):
    year, month = today.year, today.month

    for _ in range(count):
        yield dt.date(year, month, 1)

        month -= 1

        if month == 0:
            year -= 1
            month = 12


def parse_close(row):
    try:
        parts = str(row[0]).split("/")

        if len(parts) != 3:
            return None

        date = dt.date(
            int(parts[0]) + 1911,
            int(parts[1]),
            int(parts[2]),
        )

        close = float(
            str(row[6]).replace(",", "")
        )

        if not math.isfinite(close) or close <= 0:
            return None

        return date.isoformat(), close

    except (
        ValueError,
        IndexError,
        TypeError,
    ):
        return None


def existing_series(code):
    path = DEST / f"{code}.json"

    if not path.exists():
        return {}

    data = read(path)
    points = {}
    if isinstance(data, dict) and data.get('code') == code and data.get('market') == 'TWSE':
        for item in data.get('series', []) if isinstance(data.get('series'), list) else []:
            try:
                day, close = item['date'], item['close']
                dt.date.fromisoformat(day)
                if isinstance(close, (int, float)) and not isinstance(close, bool) and math.isfinite(close) and close > 0:
                    points[day] = close
            except (KeyError, TypeError, ValueError):
                pass
    return points


def atomic_write(path, data):
    # Leave the previous valid file intact if serialization/write fails.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as f:
            temporary = f.name
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--offset", type=int, default=0
    )
    parser.add_argument(
        "--limit", type=int, default=40
    )
    parser.add_argument(
        "--months", type=int, default=7
    )

    args = parser.parse_args()

    if (
        args.offset < 0
        or not 1 <= args.limit <= 60
        or not 1 <= args.months <= 12
    ):
        parser.error("Invalid batch settings")

    payload = json.loads(
        SOURCE.read_text(encoding="utf-8")
    )

    candidates = select_candidates(payload)

    batch = candidates[
        args.offset:args.offset + args.limit
    ]

    today = dt.datetime.now(
        ZoneInfo("Asia/Taipei")
    ).date()

    DEST.mkdir(
        parents=True, exist_ok=True
    )

    print(
        "TWSE candidates:",
        len(candidates),
        "offset:",
        args.offset,
        "batch:",
        len(batch),
        flush=True,
    )

    for index, row in enumerate(batch, 1):
        code = row["code"]
        points = existing_series(code)
        errors = []
        old = read(DEST / f'{code}.json')
        checks = old.get('month_checks', {}) if isinstance(old, dict) else {}
        if not isinstance(checks, dict):
            checks = {}
        checks = dict(checks)

        for month in months_back(
            today, args.months
        ):
            url = (
                "https://www.twse.com.tw/"
                "exchangeReport/STOCK_DAY"
                "?response=json"
                f"&date={month:%Y%m%d}"
                f"&stockNo={code}"
            )

            key = month.isoformat()[:7]
            try:
                data = fetch(url)
                if not isinstance(data, dict) or data.get('stat') != 'OK':
                    raise ValueError(f'Non-OK response: {data}')
                if data.get('date') != month.strftime('%Y%m%d'):
                    raise ValueError('Response month mismatch')
                title = str(data.get('title', ''))
                if code not in title.split():
                    raise ValueError('Response stock identity mismatch')
                raw_rows = data.get('data')
                if not isinstance(raw_rows, list):
                    raise ValueError('Invalid monthly response rows')
                if not raw_rows:
                    if (month == today.replace(day=1)
                        and not has_reached_first_weekday(month, today)):
                        checks[key] = {'status': 'not_started', 'checked_on': today.isoformat()}
                        time.sleep(0.45)
                        continue
                    raise ValueError('Empty monthly response; requires review')
                monthly = {}
                for item in raw_rows:
                    parsed = parse_close(item)
                    if not parsed:
                        raise ValueError('Unparseable monthly row')
                    day, close = parsed
                    if not day.startswith(key) or day > today.isoformat() or day in monthly:
                        raise ValueError('Wrong-month, future or duplicate row')
                    monthly[day] = close
                verified = [{'date': day, 'close': close} for day, close in sorted(monthly.items())]
                # Replace only after every row in this official response validates.
                points = {day: close for day, close in points.items() if not day.startswith(key)}
                points.update(monthly)
                checks[key] = {'status': 'verified', 'checked_on': today.isoformat(),
                               'count': len(verified), 'sha256': digest(verified)}
            except Exception as error:
                errors.append({'month': str(month), 'reason': str(error)})
                checks[key] = {'status': 'error', 'checked_on': today.isoformat(), 'reason': str(error)}

            time.sleep(0.45)

        series = [
            {"date": day, "close": close}
            for day, close in sorted(points.items())
        ]

        output = {
            "code": code,
            "name": row.get("name"),
            "market": "TWSE",
            "source": "TWSE STOCK_DAY",
            "price_type": "unadjusted close",
            "series": series,
            "errors": errors,
            "month_checks": checks,
            "last_attempt": dt.datetime.now(ZoneInfo('Asia/Taipei')).isoformat(),
        }

        output['validation'] = {'issues': inspect(output, code, list(months_back(today, args.months)), today)}
        output['validation']['complete'] = not output['validation']['issues']
        atomic_write(DEST / f'{code}.json', output)

        print(
            f"{index}/{len(batch)} "
            f"{code}: {len(series)} days, "
            f"{len(errors)} errors",
            flush=True,
        )

    # Rebuild the index from all saved files,
    # not just the current batch.
    manifest = []

    for path in sorted(DEST.glob("*.json")):
        if path.name == "index.json":
            continue

        try:
            data = json.loads(
                path.read_text(encoding="utf-8")
            )

            manifest.append({
                "code": data["code"],
                "complete": not inspect(data, path.stem, list(months_back(today, args.months)), today),
                "issues": inspect(data, path.stem, list(months_back(today, args.months)), today),
                "name": data.get("name"),
                "sessions": len(
                    data.get("series", [])
                ),
                "errors": len(
                    data.get("errors", [])
                ),
            })

        except (ValueError, KeyError, TypeError, AttributeError):
            manifest.append({'code': path.stem, 'complete': False, 'issues': ['unreadable_or_invalid_file']})

    summary = {
        "generated_at": dt.datetime.now(
            ZoneInfo("Asia/Taipei")
        ).isoformat(),
        "coverage": (
            "TWSE partial batch backfill; "
            "not full-market or TPEx"
        ),
        "total_twse_candidates": len(candidates),
        "saved_stocks": len(manifest),
        "complete_stocks": sum(not inspect(read(DEST / f"{row['code']}.json"), str(row['code']), list(months_back(today, args.months)), today) for row in candidates),
        "last_batch": {
            "offset": args.offset,
            "limit": args.limit,
            "months": args.months,
        },
        "price_type": (
            "unadjusted close; "
            "corporate actions not adjusted"
        ),
        "stocks": manifest,
    }

    atomic_write(DEST / 'index.json', summary)

    print(
        "Completed. Saved stocks:",
        len(manifest),
        flush=True,
    )


if __name__ == "__main__":
    main()
