"""Fail-closed TWSE cache validation and batch planning.

A verified month proves fidelity to a successful official response, not
that a stock traded every exchange session. Suspensions and listing gaps
still require separate review.
"""

import datetime as dt
import hashlib
import json
import math
import pathlib

LISTING_SOURCE = "TWSE OpenAPI t187ap03_L"


def digest(series):
    return hashlib.sha256(
        json.dumps(
            series,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def has_reached_first_weekday(month, today):
    """Compatibility helper: first weekday, not a full exchange calendar."""
    if month.year != today.year or month.month != today.month:
        return month < today.replace(day=1)

    first_weekday = month
    while first_weekday.weekday() >= 5:
        first_weekday += dt.timedelta(days=1)

    return today >= first_weekday


def month_not_started(month, today):
    """Recognize a current month before its first possible weekday."""
    return (
        month.year == today.year
        and month.month == today.month
        and not has_reached_first_weekday(month, today)
    )


def inspect(data, code, months, today):
    issues = []

    if not isinstance(data, dict):
        return ["invalid_object"]

    if data.get("code") != code or data.get("market") != "TWSE":
        issues.append("identity_mismatch")

    errors = data.get("errors")
    if not isinstance(errors, list):
        issues.append("invalid_errors")
        errors = []

    active_errors = []
    for error in errors:
        if not isinstance(error, dict):
            active_errors.append(error)
            continue

        month_value = str(error.get("month", ""))
        try:
            error_month = dt.date.fromisoformat(month_value)
        except ValueError:
            active_errors.append(error)
            continue

        if not month_not_started(error_month, today):
            active_errors.append(error)

    if active_errors:
        issues.append("fetch_errors")

    series = data.get("series")
    if not isinstance(series, list) or not series:
        return sorted(set(issues + ["empty_or_invalid_series"]))

    seen = set()
    previous = ""
    valid = []

    for item in series:
        try:
            day = item["date"]
            date = dt.date.fromisoformat(day)
            close = item["close"]

            if (
                date.isoformat() != day
                or date > today
                or day in seen
                or day <= previous
                or isinstance(close, bool)
                or not isinstance(close, (int, float))
                or not math.isfinite(close)
                or close <= 0
            ):
                raise ValueError("Invalid price row")

            seen.add(day)
            previous = day
            valid.append(item)

        except (KeyError, TypeError, ValueError):
            issues.append("invalid_duplicate_or_unsorted_row")

    ledger = data.get("month_checks", {})
    if not isinstance(ledger, dict):
        ledger = {}

    for month in months:
        key = month.isoformat()[:7]
        rows = [x for x in valid if x["date"].startswith(key)]

        check = ledger.get(key, {})
        if not isinstance(check, dict):
            check = {}

        if month_not_started(month, today):
            # No trading data is required before the first possible
            # weekday of the current month.
            if rows:
                issues.append(f"{key}:unexpected_early_rows")
            continue

        if check.get("status") == "not_listed":
            listing_value = check.get("listing_date")
            try:
                listing_date = dt.date.fromisoformat(listing_value)
                if listing_date.isoformat() != listing_value:
                    raise ValueError()
            except (TypeError, ValueError):
                issues.append(f"{key}:invalid_listing_date")
                continue

            next_month = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
            month_end = next_month - dt.timedelta(days=1)
            checked_on = check.get("checked_on")
            try:
                checked_date = dt.date.fromisoformat(checked_on)
                if checked_date.isoformat() != checked_on or checked_date > today:
                    raise ValueError()
            except (TypeError, ValueError):
                issues.append(f"{key}:invalid_listing_check_date")

            if (
                check.get("listing_source") != LISTING_SOURCE
                or month_end >= listing_date
                or rows
                or "row_count" in check
                or "no_trade_days" in check
                or "no_price_days" in check
            ):
                issues.append(f"{key}:invalid_not_listed_evidence")
            continue

        if check.get("status") != "verified":
            issues.append(f"{key}:unverified_or_missing")

        elif (
            check.get("count") != len(rows)
            or check.get("sha256") != digest(rows)
        ):
            issues.append(f"{key}:content_mismatch")
        elif not rows and "row_count" not in check:
            issues.append(f"{key}:unverified_or_missing")

        if (
            key == today.isoformat()[:7]
            and has_reached_first_weekday(month, today)
            and check.get("checked_on") != today.isoformat()
        ):
            issues.append(f"{key}:stale")

        if (
            "row_count" in check
            or "no_trade_days" in check
            or "no_price_days" in check
        ):
            no_trade_days = check.get('no_trade_days')
            no_price_days = check.get('no_price_days', [])
            row_count = check.get('row_count')
            valid_no_trade_days = []
            valid_no_price_days = []
            if isinstance(no_trade_days, list):
                for day in no_trade_days:
                    try:
                        date = dt.date.fromisoformat(day)
                        if (
                            date.isoformat() != day
                            or not day.startswith(key)
                            or date > today
                            or day in seen
                            or day in valid_no_trade_days
                        ):
                            raise ValueError()
                        valid_no_trade_days.append(day)
                    except (TypeError, ValueError):
                        issues.append(f'{key}:invalid_no_trade_day')
            else:
                issues.append(f'{key}:invalid_no_trade_day')
            if isinstance(no_price_days, list):
                for day in no_price_days:
                    try:
                        date = dt.date.fromisoformat(day)
                        if (
                            date.isoformat() != day
                            or not day.startswith(key)
                            or date > today
                            or day in seen
                            or day in valid_no_trade_days
                            or day in valid_no_price_days
                        ):
                            raise ValueError()
                        valid_no_price_days.append(day)
                    except (TypeError, ValueError):
                        issues.append(f'{key}:invalid_no_price_day')
            else:
                issues.append(f'{key}:invalid_no_price_day')
            if (
                isinstance(row_count, bool)
                or not isinstance(row_count, int)
                or row_count
                != len(rows) + len(valid_no_trade_days) + len(valid_no_price_days)
            ):
                issues.append(f'{key}:row_count_mismatch')
    return sorted(set(issues))


def read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def candidates(payload):
    if not isinstance(payload, dict):
        return []

    records = payload.get("records", [])
    if not isinstance(records, list):
        return []

    selected = {}

    for row in records:
        if not isinstance(row, dict):
            continue

        code = str(row.get("code", ""))

        if (
            row.get("market") == "TWSE"
            and row.get("common_stock_candidate")
            and code.isdigit()
            and len(code) == 4
        ):
            selected[code] = row

    return sorted(selected.values(), key=lambda row: str(row["code"]))


def plan(stocks, root, months, today, batch_size=40):
    pending = []

    for offset in range(0, len(stocks), batch_size):
        attempts = []
        incomplete = False

        for row in stocks[offset:offset + batch_size]:
            code = str(row["code"])
            data = read(root / f"{code}.json")

            if inspect(data, code, months, today):
                incomplete = True

            attempts.append(
                data.get("last_attempt", "")
                if isinstance(data, dict)
                else ""
            )

        if incomplete:
            pending.append((max(attempts, default=""), offset))

    # Prioritize the least recently attempted incomplete batch.
    return min(pending)[1] if pending else -1


if __name__ == "__main__":
    import argparse
    from backfill import ROOT, DEST, months_back
    from zoneinfo import ZoneInfo

    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--months", type=int, default=7)
    parser.add_argument("--check-batch", action="store_true")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()

    if (
        not 1 <= args.months <= 12
        or args.offset < 0
        or not 1 <= args.limit <= 60
    ):
        parser.error("Invalid batch settings")

    today = dt.datetime.now(ZoneInfo("Asia/Taipei")).date()
    stocks = candidates(read(ROOT / "data.json"))

    if not stocks:
        raise SystemExit(
            "No TWSE candidates: refusing to report completion"
        )

    months = list(months_back(today, args.months))

    if args.check_batch:
        stocks = stocks[args.offset:args.offset + args.limit]
        if not stocks:
            raise SystemExit("Empty batch")

    if args.plan:
        print(f"offset={plan(stocks, DEST, months, today)}")
        print(f"total={len(stocks)}")

    else:
        report = {
            str(row["code"]): inspect(
                read(DEST / (str(row["code"]) + ".json")),
                str(row["code"]),
                months,
                today,
            )
            for row in stocks
        }

        print(
            json.dumps(
                {
                    "total": len(stocks),
                    "complete": sum(not x for x in report.values()),
                    "incomplete": {
                        k: v for k, v in report.items() if v
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        if args.check_batch and any(report.values()):
            raise SystemExit(1)
