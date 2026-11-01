"""Batched TPEx monthly historical close backfill."""

import argparse
import datetime as dt
import json
import math
import pathlib
import time
from zoneinfo import ZoneInfo

from backfill import (
    ROOT,
    atomic_write,
    fetch,
    months_back,
    parse_integer_field,
)
from backfill_integrity import (
    candidates,
    digest,
    has_reached_first_weekday,
    inspect,
    month_not_started,
    read,
    TPEX_LISTING_SOURCE,
)

SOURCE = ROOT / "data.json"
DEST = ROOT / "historical" / "tpex"
SOURCE_URL = (
    "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
)


def parse_monthly_observation(row, listing_marker_allowed=False):
    if not isinstance(row, (list, tuple)) or len(row) != 9:
        return None

    try:
        raw_date = str(row[0]).strip()
        has_listing_marker = raw_date.endswith("*")
        if "*" in raw_date and (
            not has_listing_marker or raw_date.count("*") != 1
            or not listing_marker_allowed
        ):
            return None
        parts = raw_date.removesuffix("*").split("/")
        if len(parts) != 3:
            return None

        year, month, day = map(int, parts)
        if year < 1911:
            year += 1911
        date = dt.date(year, month, day)
        if date.weekday() >= 5:
            return None
        volume = parse_integer_field(row[1])
        amount = parse_integer_field(row[2])
        trades = parse_integer_field(row[8])
        if any(value is None or value < 0 for value in (volume, amount, trades)):
            return None

        price_fields = [str(value).strip() for value in row[3:7]]
        if price_fields == ["--"] * 4:
            change = float(str(row[7]).strip().replace(",", ""))
            if not math.isfinite(change) or change != 0:
                return None
            if volume == amount == trades == 0:
                return date.isoformat(), None, "no_trade", has_listing_marker
            if trades == 0:
                return None
            return date.isoformat(), None, "no_price", has_listing_marker
        if any(value == "--" for value in price_fields):
            return None
        open_price, high, low, close = (
            float(value.replace(",", "")) for value in price_fields
        )
        if (
            any(
                not math.isfinite(price) or price <= 0
                for price in (open_price, high, low, close)
            )
            or low > min(open_price, close)
            or high < max(open_price, close)
            or volume <= 0
            or amount <= 0
            or trades <= 0
        ):
            return None
        return date.isoformat(), close, "quote", has_listing_marker
    except (TypeError, ValueError):
        return None


def parse_monthly_row(row):
    observation = parse_monthly_observation(row)
    if observation is None or observation[2] != "quote":
        return None
    return observation[:2]


def fetch_month(code, month):
    expected_date = month.strftime("%Y%m01")
    url = (
        f"{SOURCE_URL}?date={month:%Y/%m}/01"
        f"&code={code}"
    )
    data = fetch(url)

    if not isinstance(data, dict) or data.get("stat") != "ok":
        raise ValueError(f"TPEx API returned non-OK response: {data}")
    if (
        data.get("date") != expected_date
        or str(data.get("code", "")).strip() != code
    ):
        raise ValueError("TPEx response month or stock identity mismatch")

    tables = data.get("tables")
    if not isinstance(tables, list) or not tables:
        raise ValueError("TPEx response has no quote table")
    table = tables[0]
    if not isinstance(table, dict):
        raise ValueError("Invalid TPEx quote table")
    if (
        table.get("title") != "個股日成交資訊"
        or table.get("date") != expected_date
    ):
        raise ValueError("TPEx quote table title or month mismatch")

    rows = table.get("data")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Empty TPEx monthly response; requires review")
    if (
        "totalCount" in table
        and table["totalCount"] != len(rows)
    ):
        raise ValueError("TPEx monthly row count mismatch")

    notes = table.get("notes", [])
    listing_marker_allowed = (
        isinstance(notes, list)
        and any(
            isinstance(note, str)
            and "表示該日為該股票上櫃掛牌首日" in note
            for note in notes
        )
    )
    result = {}
    no_trade_days = []
    no_trade_observations = []
    no_price_observations = []
    listing_dates = []
    previous_day = ""
    month_key = month.isoformat()[:7]
    today = dt.datetime.now(ZoneInfo("Asia/Taipei")).date()
    for row in rows:
        parsed = parse_monthly_observation(row, listing_marker_allowed)
        if parsed is None:
            raise ValueError("Unparseable or invalid TPEx quote row")
        day, close, observation_type, has_listing_marker = parsed
        if (
            not day.startswith(month_key)
            or day > today.isoformat()
            or day <= previous_day
            or day in result
        ):
            raise ValueError("Wrong-month, future, duplicate or unsorted TPEx row")
        if observation_type == "quote":
            result[day] = close
        elif observation_type == "no_trade":
            no_trade_days.append(day)
            no_trade_observations.append({
                "date": day,
                "volume": parse_integer_field(row[1]),
                "amount": parse_integer_field(row[2]),
                "trades": parse_integer_field(row[8]),
            })
        else:
            no_price_observations.append({
                "date": day,
                "volume": parse_integer_field(row[1]),
                "amount": parse_integer_field(row[2]),
                "trades": parse_integer_field(row[8]),
            })
        if has_listing_marker:
            listing_dates.append(day)
        previous_day = day

    if len(listing_dates) > 1:
        raise ValueError("Multiple TPEx first-listing markers in one month")
    return {
        "quotes": result,
        "name": str(data.get("name", "")).strip(),
        "no_trade_days": no_trade_days,
        "no_trade_observations": no_trade_observations,
        "no_price_observations": no_price_observations,
        "listing_date": listing_dates[0] if listing_dates else None,
        "source_rows_sha256": digest(rows),
    }


def parse_error_month(error):
    if not isinstance(error, dict):
        return None
    try:
        return dt.date.fromisoformat(str(error.get("month", ""))).replace(day=1)
    except ValueError:
        return None


def month_is_verified(data, code, month, today):
    if not isinstance(data, dict):
        return False
    key = month.isoformat()[:7]
    checks = data.get("month_checks")
    if not isinstance(checks, dict) or not isinstance(data.get("errors"), list):
        return False
    check = checks.get(key)
    if not isinstance(check, dict) or check.get("status") != "verified":
        return False
    if any(parse_error_month(error) == month for error in data.get("errors", [])):
        return False
    candidate = dict(data)
    candidate["errors"] = []
    return not inspect(candidate, code, [month], today, market="TPEx")


def update_stock(
    row,
    stock_months,
    today,
    validation_months,
    refresh_verified=False,
):
    code = str(row["code"])
    path = DEST / f"{code}.json"
    old = read(path)
    points = {}
    if (
        isinstance(old, dict)
        and old.get("code") == code
        and old.get("market") == "TPEx"
        and isinstance(old.get("series"), list)
    ):
        for item in old["series"]:
            try:
                date = dt.date.fromisoformat(item["date"])
                close = item["close"]
                if (
                    date.isoformat() == item["date"]
                    and isinstance(close, (int, float))
                    and not isinstance(close, bool)
                    and math.isfinite(close)
                    and close > 0
                ):
                    points[item["date"]] = close
            except (KeyError, TypeError, ValueError):
                continue

    previous_errors = old.get("errors", []) if isinstance(old, dict) else []
    checks = old.get("month_checks", {}) if isinstance(old, dict) else {}
    if not isinstance(checks, dict):
        checks = {}
    checks = dict(checks)

    month_keys = {month.isoformat()[:7] for month in stock_months}
    if not isinstance(previous_errors, list):
        errors = [{"month": "", "reason": "Invalid prior errors structure"}]
    else:
        errors = []
        for error in previous_errors:
            error_month = parse_error_month(error)
            if (
                error_month is None
                or error_month.isoformat()[:7] not in month_keys
            ):
                errors.append(error)

    official_name = ""
    listing_date = None
    if isinstance(old, dict) and isinstance(old.get("month_checks"), dict):
        old_series_days = {
            item.get("date")
            for item in old.get("series", [])
            if isinstance(item, dict)
        } if isinstance(old.get("series"), list) else set()
        for checked_month, check in old["month_checks"].items():
            if (
                isinstance(check, dict)
                and check.get("status") == "verified"
                and check.get("listing_source") == TPEX_LISTING_SOURCE
            ):
                try:
                    candidate_date = dt.date.fromisoformat(
                        check.get("listing_date")
                    )
                except (TypeError, ValueError):
                    continue
                if (
                    candidate_date.isoformat().startswith(checked_month)
                    and candidate_date.isoformat() in old_series_days
                    and month_is_verified(
                        old, code, candidate_date.replace(day=1), today
                    )
                ):
                    listing_date = candidate_date
                    break

    for month in stock_months:
        key = month.isoformat()[:7]
        if month_not_started(month, today):
            checks[key] = {
                "status": "not_started",
                "checked_on": today.isoformat(),
            }
            continue
        next_month = (
            month.replace(day=28) + dt.timedelta(days=4)
        ).replace(day=1)
        month_end = next_month - dt.timedelta(days=1)
        if listing_date and month_end < listing_date:
            checks[key] = {
                "status": "not_listed",
                "checked_on": today.isoformat(),
                "listing_date": listing_date.isoformat(),
                "listing_source": TPEX_LISTING_SOURCE,
            }
            errors = [
                error for error in errors
                if parse_error_month(error) != month
            ]
            print(
                f"{code} {key}: verified pre-listing month "
                f"(listed {listing_date.isoformat()})"
            )
            continue
        if (
            not refresh_verified
            and month != today.replace(day=1)
            and month_is_verified(old, code, month, today)
        ):
            print(f"{code} {key}: preserved previously verified quotes")
            continue

        try:
            result = fetch_month(code, month)
            monthly = result["quotes"]
            official_name = result["name"]
            verified = [
                {"date": day, "close": close}
                for day, close in sorted(monthly.items())
            ]
            points = {
                day: close
                for day, close in points.items()
                if not day.startswith(key)
            }
            points.update(monthly)
            checks[key] = {
                "status": "verified",
                "checked_on": today.isoformat(),
                "count": len(verified),
                "row_count": (
                    len(verified)
                    + len(result["no_trade_days"])
                    + len(result["no_price_observations"])
                ),
                "no_trade_days": result["no_trade_days"],
                "no_trade_observations": result["no_trade_observations"],
                "no_price_days": [
                    item["date"]
                    for item in result["no_price_observations"]
                ],
                "no_price_observations": result["no_price_observations"],
                "source_rows_sha256": result["source_rows_sha256"],
                "sha256": digest(verified),
            }
            if result["listing_date"]:
                listing_date = dt.date.fromisoformat(result["listing_date"])
                checks[key]["listing_date"] = result["listing_date"]
                checks[key]["listing_source"] = TPEX_LISTING_SOURCE
            errors = [
                error for error in errors
                if parse_error_month(error) != month
            ]
            print(
                f"{code} {key}: verified {len(verified)} official quotes "
                f"({len(result['no_price_observations'])} no-price, "
                f"{len(result['no_trade_days'])} no-trade)"
            )
        except (KeyError, RuntimeError, ValueError, TypeError, OSError) as error:
            checks[key] = {
                "status": "error",
                "checked_on": today.isoformat(),
                "reason": str(error),
            }
            errors.append({"month": month.isoformat(), "reason": str(error)})
            print(f"{code} {key}: ERROR {error}", flush=True)
            official_name = ""
        time.sleep(0.45)

    series = [
        {"date": day, "close": close}
        for day, close in sorted(points.items())
    ]
    output = {
        "code": code,
        "name": (
            official_name
            if official_name
            else (old.get("name") if isinstance(old, dict) else row.get("name"))
        ),
        "market": "TPEx",
        "source": "TPEx 個股日成交資訊",
        "source_url": SOURCE_URL,
        "price_type": "unadjusted close",
        "series": series,
        "errors": errors,
        "month_checks": checks,
        "last_attempt": dt.datetime.now(ZoneInfo("Asia/Taipei")).isoformat(),
    }
    issues = inspect(output, code, validation_months, today, market="TPEx")
    output["validation"] = {
        "issues": issues,
        "complete": not issues,
    }
    atomic_write(path, output)
    return output


def write_indexes(stocks, months, today, offset, limit):
    DEST.mkdir(parents=True, exist_ok=True)
    source_data = read(SOURCE)
    source_meta = (
        source_data.get("sources", {}).get("TPEx", {})
        if isinstance(source_data, dict)
        else {}
    )
    roster = [
        {
            "code": str(row["code"]),
            "name": row.get("name", ""),
            "market": "TPEx",
        }
        for row in stocks
    ]
    atomic_write(
        DEST / "stocks.json",
        {
            "generated_at": dt.datetime.now(ZoneInfo("Asia/Taipei")).isoformat(),
            "source": source_meta.get("url"),
            "source_fetched_at": (
                source_data.get("fetched_at")
                if isinstance(source_data, dict)
                else None
            ),
            "count": len(roster),
            "stocks": roster,
        },
    )

    manifest = []
    for row in stocks:
        code = str(row["code"])
        data = read(DEST / f"{code}.json")
        issues = inspect(data, code, months, today, market="TPEx")
        manifest.append({
            "code": code,
            "name": row.get("name"),
            "complete": not issues,
            "issues": issues,
            "sessions": (
                len(data.get("series", [])) if isinstance(data, dict) else 0
            ),
            "errors": (
                len(data.get("errors", [])) if isinstance(data, dict) else 0
            ),
            "last_attempt": (
                data.get("last_attempt") if isinstance(data, dict) else None
            ),
        })

    atomic_write(
        DEST / "index.json",
        {
            "generated_at": dt.datetime.now(ZoneInfo("Asia/Taipei")).isoformat(),
            "market": "TPEx",
            "coverage": f"TPEx official monthly quotes; rolling {len(months)} months",
            "total_tpex_candidates": len(stocks),
            "saved_stocks": sum(
                (DEST / f"{row['code']}.json").exists() for row in stocks
            ),
            "complete_stocks": sum(item["complete"] for item in manifest),
            "last_batch": {
                "offset": offset,
                "limit": limit,
                "months": len(months),
            },
            "price_type": "unadjusted close; corporate actions not adjusted",
            "stocks": manifest,
        },
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--months", type=int, default=7)
    parser.add_argument("--code", help="Backfill one TPEx stock")
    parser.add_argument("--month", help="Backfill one month in YYYY-MM format")
    args = parser.parse_args(argv)
    if args.offset < 0 or not 1 <= args.limit <= 60 or not 1 <= args.months <= 12:
        parser.error("Invalid batch settings")
    if (args.code is None) != (args.month is None):
        parser.error("--code and --month must be provided together")

    target_month = None
    if args.month:
        try:
            target_month = dt.date.fromisoformat(f"{args.month}-01")
        except ValueError:
            parser.error("--month must be a valid YYYY-MM month")
        if target_month.strftime("%Y-%m") != args.month:
            parser.error("--month must be a valid YYYY-MM month")

    payload = read(SOURCE)
    stocks = candidates(payload, market="TPEx")
    if not stocks:
        raise SystemExit("No TPEx candidates: refusing to report completion")
    if target_month:
        today = dt.datetime.now(ZoneInfo("Asia/Taipei")).date()
        if target_month > today.replace(day=1):
            parser.error("--month cannot be in the future")
        batch = [row for row in stocks if str(row["code"]) == args.code]
        if not batch:
            parser.error(f"--code {args.code} is not a current TPEx candidate")
        months = [target_month]
    else:
        today = dt.datetime.now(ZoneInfo("Asia/Taipei")).date()
        months = list(months_back(today, args.months))
        batch = stocks[args.offset:args.offset + args.limit]

    if not batch:
        parser.error("Selected TPEx batch is empty")
    DEST.mkdir(parents=True, exist_ok=True)
    print(
        f"TPEx candidates: {len(stocks)} offset: {args.offset} "
        f"batch: {len(batch)}",
        flush=True,
    )
    for index, row in enumerate(batch, 1):
        stock_months = list(months)
        if target_month is None:
            old = read(DEST / f"{row['code']}.json")
            old_errors = old.get("errors", []) if isinstance(old, dict) else []
            if isinstance(old_errors, list):
                for error in old_errors:
                    error_month = parse_error_month(error)
                    if error_month and error_month not in stock_months:
                        stock_months.append(error_month)
        validation_months = list(months_back(today, args.months))
        if target_month and target_month not in validation_months:
            validation_months.append(target_month)
        if target_month is None:
            validation_months.extend(
                month for month in stock_months if month not in validation_months
            )
        update_stock(
            row,
            stock_months,
            today,
            validation_months=validation_months,
            refresh_verified=target_month is not None,
        )
        print(f"{index}/{len(batch)} {row['code']}: saved", flush=True)

    index_months = list(months_back(today, args.months))
    if target_month and target_month not in index_months:
        index_months.append(target_month)
    write_indexes(stocks, index_months, today, args.offset, args.limit)
    print(f"TPEx progress index updated: {DEST / 'index.json'}", flush=True)


if __name__ == "__main__":
    main()
