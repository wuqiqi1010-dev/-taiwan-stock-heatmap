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
from backfill_integrity import (
    LISTING_SOURCE,
    candidates as select_candidates,
    digest,
    has_reached_first_weekday,
    inspect,
    month_not_started,
    read,
)
import pathlib
import socket
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data.json"
DEST = ROOT / "historical" / "twse"
LISTING_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"

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
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = response.read()
            return json.loads(raw.decode("utf-8-sig"))
        except (
            http.client.IncompleteRead,
            urllib.error.URLError,
            socket.timeout,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
        ) as error:
            last_error = error
            print(f"Retry {attempt}/4: {error}", flush=True)
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


def fetch_listing_dates(codes):
    rows = fetch(LISTING_URL)
    if not isinstance(rows, list) or not rows:
        raise ValueError("Invalid TWSE listing-date response")

    wanted = {str(code) for code in codes}
    dates = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("公司代號", "")).strip()
        if code not in wanted:
            continue

        value = str(row.get("上市日期", "")).strip()
        if len(value) != 8 or not value.isdigit():
            raise ValueError(f"Invalid official listing date for {code}: {value!r}")
        try:
            listing_date = dt.datetime.strptime(value, "%Y%m%d").date()
        except ValueError as error:
            raise ValueError(
                f"Invalid official listing date for {code}: {value!r}"
            ) from error

        if code in dates and dates[code] != listing_date:
            raise ValueError(f"Conflicting official listing dates for {code}")
        dates[code] = listing_date

    return dates


def parse_integer_field(value):
    text = str(value).strip()
    parts = text.split(",")
    if not parts[0].isdigit() or (
        len(parts) > 1
        and (
            len(parts[0]) > 3
            or any(len(part) != 3 or not part.isdigit() for part in parts[1:])
        )
    ):
        return None
    return int("".join(parts))


def parse_error_month(error):
    if not isinstance(error, dict):
        return None
    try:
        return dt.date.fromisoformat(str(error.get("month", ""))).replace(day=1)
    except ValueError:
        return None


def parse_monthly_row(row):
    if not isinstance(row, (list, tuple)) or len(row) != 10:
        return None

    try:
        parts = str(row[0]).split("/")
        if len(parts) != 3:
            return None

        date = dt.date(
            int(parts[0]) + 1911,
            int(parts[1]),
            int(parts[2]),
        )

        day = date.isoformat()
        close_text = str(row[6]).strip().replace(",", "")
        if close_text == "--":
            volume = parse_integer_field(row[1])
            amount = parse_integer_field(row[2])
            trades = parse_integer_field(row[8])
            prices = [str(value).strip() for value in row[3:7]]
            change = str(row[7]).strip()
            remark = str(row[9]).strip()
            if prices == ["--"] * 4 and change == "0.00" and not remark:
                if volume == amount == trades == 0:
                    return day, None, "no_trade"
                if (
                    volume is not None and volume > 0
                    and amount is not None and amount > 0
                    and trades is not None and trades > 0
                ):
                    return day, None, "no_price"
            return None

        close = float(close_text)
        if not math.isfinite(close) or close <= 0:
            return None

        return day, close, None

    except (
        ValueError,
        IndexError,
        TypeError,
    ):
        return None


def parse_close(row):
    parsed = parse_monthly_row(row)
    if parsed is None or parsed[1] is None:
        return None
    return parsed[:2]


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
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--months", type=int, default=7)
    parser.add_argument("--code", help="Backfill one TWSE stock")
    parser.add_argument("--month", help="Backfill one month in YYYY-MM format")
    args = parser.parse_args()
    if args.offset < 0 or not 1 <= args.limit <= 60 or not 1 <= args.months <= 12:
        parser.error("Invalid batch settings")
    if (args.code is None) != (args.month is None):
        parser.error("--code and --month must be provided together")

    target_month = None
    if args.month is not None:
        try:
            target_month = dt.date.fromisoformat(f"{args.month}-01")
        except ValueError:
            parser.error("--month must be a valid YYYY-MM month")
        if target_month.strftime("%Y-%m") != args.month:
            parser.error("--month must be a valid YYYY-MM month")

    payload = json.loads(SOURCE.read_text(encoding="utf-8"))
    candidates = select_candidates(payload)
    if args.code is None:
        batch = candidates[args.offset:args.offset + args.limit]
    else:
        batch = [row for row in candidates if str(row["code"]) == args.code]
        if not batch:
            parser.error(f"--code {args.code} is not a current TWSE candidate")
    today = dt.datetime.now(ZoneInfo("Asia/Taipei")).date()
    if target_month and target_month > today.replace(day=1):
        parser.error("--month cannot be in the future")
    selected_months = (
        [target_month] if target_month else list(months_back(today, args.months))
    )
    listing_months = list(selected_months)
    if not target_month:
        for row in batch:
            old = read(DEST / f'{row["code"]}.json')
            old_errors = old.get("errors", []) if isinstance(old, dict) else []
            if isinstance(old_errors, list):
                for error in old_errors:
                    error_month = parse_error_month(error)
                    if error_month and error_month not in listing_months:
                        listing_months.append(error_month)
    listing_dates = {}
    if batch and any(not month_not_started(month, today) for month in listing_months):
        try:
            listing_dates = fetch_listing_dates(row["code"] for row in batch)
        except (RuntimeError, ValueError) as error:
            print(
                "TWSE listing-date lookup failed; non-OK monthly responses "
                f"will remain errors: {error}",
                flush=True,
            )
        missing_listing_dates = sorted(
            str(row["code"]) for row in batch
            if str(row["code"]) not in listing_dates
        )
        if missing_listing_dates:
            print(
                "No official listing date for: "
                + ", ".join(missing_listing_dates)
                + "; non-OK monthly responses will remain errors",
                flush=True,
            )
    DEST.mkdir(parents=True, exist_ok=True)
    print(
        "TWSE candidates:", len(candidates), "offset:", args.offset,
        "batch:", len(batch),
        "target:", f"{args.code}/{args.month}" if target_month else "batch",
        flush=True,
    )

    for index, row in enumerate(batch, 1):
        code = row["code"]
        points = existing_series(code)
        old = read(DEST / f'{code}.json')
        previous_errors = old.get("errors", []) if isinstance(old, dict) else []
        if target_month:
            target_key = target_month.isoformat()[:7]
            errors = [
                error for error in previous_errors if
                not isinstance(error, dict)
                or str(error.get("month", ""))[:7] != target_key
            ] if isinstance(previous_errors, list) else []
            stock_months = list(selected_months)
        else:
            stock_months = list(selected_months)
            if isinstance(previous_errors, list):
                for error in previous_errors:
                    error_month = parse_error_month(error)
                    if error_month and error_month not in stock_months:
                        stock_months.append(error_month)
                stock_month_keys = {month.isoformat()[:7] for month in stock_months}
                errors = []
                for error in previous_errors:
                    error_month = parse_error_month(error)
                    if (
                        error_month is None
                        or error_month.isoformat()[:7] not in stock_month_keys
                    ):
                        errors.append(error)
            else:
                errors = [{'month': '', 'reason': 'Invalid prior errors structure'}]
        checks = old.get('month_checks', {}) if isinstance(old, dict) else {}
        if not isinstance(checks, dict):
            checks = {}
        checks = dict(checks)

        for month in stock_months:
            key = month.isoformat()[:7]
            listing_date = listing_dates.get(str(code))
            if listing_date:
                next_month = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
                month_end = next_month - dt.timedelta(days=1)
                if month_end < listing_date:
                    checks[key] = {
                        "status": "not_listed",
                        "checked_on": today.isoformat(),
                        "listing_date": listing_date.isoformat(),
                        "listing_source": LISTING_SOURCE,
                    }
                    errors = [
                        error for error in errors
                        if not isinstance(error, dict)
                        or str(error.get("month", ""))[:7] != key
                    ]
                    print(
                        f"{code} {key}: verified pre-listing month "
                        f"(listed {listing_date.isoformat()})",
                        flush=True,
                    )
                    continue

            # No trading weekday has occurred in this month yet.
            # Do not fetch an expected empty TWSE monthly response.
            if (month == today.replace(day=1)
                    and not has_reached_first_weekday(month, today)):
                checks[key] = {'status': 'not_started', 'checked_on': today.isoformat()}
                continue

            url = (
                "https://www.twse.com.tw/"
                "exchangeReport/STOCK_DAY"
                "?response=json"
                f"&date={month:%Y%m%d}"
                f"&stockNo={code}"
            )
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
                    raise ValueError('Empty monthly response; requires review')
                monthly = {}
                no_trade_days = []
                no_price_days = []
                for item in raw_rows:
                    parsed = parse_monthly_row(item)
                    if not parsed:
                        raise ValueError('Unparseable monthly row')
                    day, close, row_kind = parsed
                    if (
                        not day.startswith(key)
                        or day > today.isoformat()
                        or day in monthly
                        or day in no_trade_days
                        or day in no_price_days
                    ):
                        raise ValueError('Wrong-month, future or duplicate row')
                    if close is None:
                        if row_kind == "no_trade":
                            no_trade_days.append(day)
                        elif row_kind == "no_price":
                            no_price_days.append(day)
                        else:
                            raise ValueError("Unclassified row without a closing price")
                    else:
                        monthly[day] = close
                verified = [{'date': day, 'close': close} for day, close in sorted(monthly.items())]
                points = {day: close for day, close in points.items() if not day.startswith(key)}
                points.update(monthly)
                checks[key] = {
                    'status': 'verified',
                    'checked_on': today.isoformat(),
                    'count': len(verified),
                    'row_count': len(raw_rows),
                    'no_trade_days': sorted(no_trade_days),
                    'no_price_days': sorted(no_price_days),
                    'sha256': digest(verified),
                }
            except (RuntimeError, ValueError, TypeError) as error:
                errors.append({'month': str(month), 'reason': str(error)})
                checks[key] = {'status': 'error', 'checked_on': today.isoformat(), 'reason': str(error)}
            time.sleep(0.45)

        series = [{'date': day, 'close': close} for day, close in sorted(points.items())]
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
        validation_months = stock_months
        output['validation'] = {
            'issues': inspect(output, code, validation_months, today)
        }
        output['validation']['complete'] = not output['validation']['issues']
        atomic_write(DEST / f'{code}.json', output)
        if target_month:
            index_path = DEST / "index.json"
            summary = read(index_path)
            if isinstance(summary, dict) and isinstance(summary.get("stocks"), list):
                entry = next(
                    (item for item in summary["stocks"]
                     if isinstance(item, dict) and str(item.get("code")) == str(code)),
                    None,
                )
                if entry is not None:
                    was_complete = entry.get("complete") is True
                    is_complete = output["validation"]["complete"]
                    entry.update({
                        "complete": is_complete,
                        "issues": output["validation"]["issues"],
                        "sessions": len(series),
                        "errors": len(errors),
                    })
                    complete_stocks = summary.get("complete_stocks")
                    if isinstance(complete_stocks, int) and not isinstance(complete_stocks, bool):
                        summary["complete_stocks"] = (
                            complete_stocks + int(is_complete) - int(was_complete)
                        )
                    else:
                        summary["complete_stocks"] = sum(
                            item.get("complete") is True
                            for item in summary["stocks"]
                            if isinstance(item, dict)
                        )
                    summary["generated_at"] = dt.datetime.now(
                        ZoneInfo("Asia/Taipei")
                    ).isoformat()
                    atomic_write(index_path, summary)
        print(f"{index}/{len(batch)} {code}: {len(series)} days, {len(errors)} errors", flush=True)

    if not target_month:
        manifest = []
        for path in sorted(DEST.glob("*.json")):
            if path.name == "index.json":
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                issues = inspect(data, path.stem, list(months_back(today, args.months)), today)
                manifest.append({
                    "code": data["code"], "complete": not issues,
                    "issues": issues, "name": data.get("name"),
                    "sessions": len(data.get("series", [])),
                    "errors": len(data.get("errors", [])),
                })
            except (ValueError, KeyError, TypeError, AttributeError):
                manifest.append({'code': path.stem, 'complete': False, 'issues': ['unreadable_or_invalid_file']})

        summary = {
            "generated_at": dt.datetime.now(ZoneInfo("Asia/Taipei")).isoformat(),
            "coverage": "TWSE partial batch backfill; not full-market or TPEx",
            "total_twse_candidates": len(candidates),
            "saved_stocks": len(manifest),
            "complete_stocks": sum(
                not inspect(read(DEST / f"{row['code']}.json"), str(row['code']),
                            list(months_back(today, args.months)), today)
                for row in candidates
            ),
            "last_batch": {"offset": args.offset, "limit": args.limit, "months": args.months},
            "price_type": "unadjusted close; corporate actions not adjusted",
            "stocks": manifest,
        }
        atomic_write(DEST / 'index.json', summary)
        print("Completed. Saved stocks:", len(manifest), flush=True)


if __name__ == "__main__":
    main()
