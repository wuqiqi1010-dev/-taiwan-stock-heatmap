
"""Validate and archive Taiwan stock market snapshots.

Fail closed: never count an unverified date.
"""

import datetime as dt
import hashlib
import http.client
import json
import pathlib
import socket
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data.json"
DEST = ROOT / "history"
INDEX = DEST / "index.json"
TZ = ZoneInfo("Asia/Taipei")

TPEX_URL = (
    "https://www.tpex.org.tw/openapi/v1/"
    "tpex_mainboard_daily_close_quotes"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Accept-Encoding": "identity",
    "Connection": "close",
}


def fetch_json(url, attempts=5):
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(
                url, headers=HEADERS
            )
            with urllib.request.urlopen(
                req, timeout=90
            ) as response:
                raw = response.read()

            result = json.loads(
                raw.decode("utf-8-sig")
            )
            print(
                "Download OK:",
                len(raw),
                "bytes",
                flush=True,
            )
            return result

        except (
            http.client.IncompleteRead,
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
            ConnectionError,
            OSError,
            ValueError,
        ) as error:
            print(
                f"Download failed {attempt}/{attempts}:",
                repr(error),
                flush=True,
            )
            if attempt == attempts:
                raise
            time.sleep(min(attempt * 4, 16))


def numeric(value):
    if value is None:
        return None
    try:
        return float(
            str(value).strip().replace(",", "")
        )
    except (TypeError, ValueError):
        return None


def official_date(value):
    text = str(value or "").strip()
    text = text.replace("-", "/")

    parts = text.split("/")

    try:
        if len(parts) == 3:
            year, month, day = map(int, parts)
            if year < 1911:
                year += 1911
            return dt.date(year, month, day)

        if text.isdigit():
            if len(text) == 7:
                return dt.date(
                    int(text[:3]) + 1911,
                    int(text[3:5]),
                    int(text[5:7]),
                )
            if len(text) == 8:
                return dt.date(
                    int(text[:4]),
                    int(text[4:6]),
                    int(text[6:8]),
                )
    except ValueError:
        return None

    return None


def first(row, names):
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def verify_twse(records, day):
    lookup = {
        str(r["code"]): r
        for r in records
        if r.get("market") == "TWSE"
    }

    checks = {}

    for code in ("2330", "2317"):
        observed = lookup.get(code)

        if observed is None:
            checks[code] = {
                "matched": False,
                "error": "Missing stock in snapshot",
            }
            continue

        url = (
            "https://www.twse.com.tw/"
            "exchangeReport/STOCK_DAY"
            f"?response=json&date={day:%Y%m%d}"
            f"&stockNo={code}"
        )

        try:
            data = fetch_json(url)
            status = data.get("stat")
            official = None
            found_date = False

            for row in data.get("data", []):
                if (
                    not isinstance(row, list)
                    or len(row) < 7
                ):
                    continue

                if official_date(row[0]) == day:
                    found_date = True
                    official = numeric(row[6])
                    break

            current = numeric(
                observed.get("close")
            )

            matched = (
                status == "OK"
                and found_date
                and official is not None
                and current is not None
                and abs(official - current) < 0.011
            )

            checks[code] = {
                "api_status": status,
                "date_found": found_date,
                "official_close": official,
                "snapshot_close": current,
                "matched": matched,
            }

        except Exception as error:
            checks[code] = {
                "matched": False,
                "error": repr(error),
            }

    passed = all(
        checks.get(code, {}).get("matched") is True
        for code in ("2330", "2317")
    )

    return passed, checks


def verify_tpex(records, day):
    lookup = {
        str(r["code"]): r
        for r in records
        if r.get("market") == "TPEx"
    }

    checks = {}

    try:
        rows = fetch_json(TPEX_URL)

        if not isinstance(rows, list):
            raise ValueError(
                "TPEx response is not a list"
            )

        print(
            "TPEx response rows:",
            len(rows),
            flush=True,
        )

        sample = next(
            (r for r in rows if isinstance(r, dict)),
            {},
        )

        print(
            "TPEx available fields:",
            list(sample.keys()),
            flush=True,
        )

        for code in ("8086", "8069"):
            official_row = None

            for row in rows:
                if not isinstance(row, dict):
                    continue

                row_code = str(first(row, [
                    "SecuritiesCompanyCode",
                    "SecuritiesCode",
                    "Code",
                    "股票代號",
                    "代號",
                ]) or "").strip()

                if row_code == code:
                    official_row = row
                    break

            observed = lookup.get(code)

            if official_row is None or observed is None:
                checks[code] = {
                    "matched": False,
                    "error": "Reference stock missing",
                }
                continue

            raw_date = first(
                official_row,
                ["Date", "TradeDate", "資料日期"],
            )
            date = official_date(raw_date)

            close = numeric(first(
                official_row,
                ["Close", "ClosingPrice", "收盤"],
            ))

            snapshot_close = numeric(
                observed.get("close")
            )

            matched = (
                date == day
                and close is not None
                and snapshot_close is not None
                and abs(close - snapshot_close) < 0.011
            )

            checks[code] = {
                "raw_date": raw_date,
                "official_date": (
                    date.isoformat() if date else None
                ),
                "official_close": close,
                "snapshot_close": snapshot_close,
                "matched": matched,
            }

    except Exception as error:
        checks["error"] = repr(error)

    passed = all(
        checks.get(code, {}).get("matched") is True
        for code in ("8086", "8069")
    )

    return passed, checks


def main():
    data = json.loads(
        SOURCE.read_text(encoding="utf-8")
    )

    records = data.get("records", [])
    sources = data.get("sources", {})

    if not isinstance(records, list) or len(records) < 500:
        raise SystemExit("Incomplete records")

    if not all(
        sources.get(m, {}).get("ok")
        for m in ("TWSE", "TPEx")
    ):
        raise SystemExit("Official sources incomplete")

    fetched = dt.datetime.fromisoformat(
        data["fetched_at"]
    )

    if fetched.tzinfo is None:
        raise SystemExit("Missing timezone")

    local = fetched.astimezone(TZ)

    if local.hour < 16 or local.weekday() >= 5:
        print("Outside archive window")
        return

    day = local.date()

    records = sorted(
        records,
        key=lambda r: (
            str(r.get("market", "")),
            str(r.get("code", "")),
        ),
    )

    digest = hashlib.sha256(
        json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    twse_ok, twse_checks = verify_twse(
        records, day
    )

    tpex_ok, tpex_checks = verify_tpex(
        records, day
    )

    verified = twse_ok and tpex_ok

    print(
        "TWSE checks:",
        json.dumps(
            twse_checks, ensure_ascii=False
        ),
        flush=True,
    )
    print(
        "TPEx checks:",
        json.dumps(
            tpex_checks, ensure_ascii=False
        ),
        flush=True,
    )

    DEST.mkdir(exist_ok=True)

    filename = day.isoformat() + ".json"
    path = DEST / filename

    archive = {
        "observation_date": day.isoformat(),
        "fetched_at": data["fetched_at"],
        "verified_market_date": (
            day.isoformat() if verified else None
        ),
        "eligible_for_trading_day_returns": verified,
        "twse_reference_date_verified": twse_ok,
        "twse_reference_checks": twse_checks,
        "tpex_date_verified": tpex_ok,
        "tpex_reference_checks": tpex_checks,
        "quotes_sha256": digest,
        "record_count": len(records),
        "records": records,
    }

    replace = True

    if path.exists():
        old = json.loads(
            path.read_text(encoding="utf-8")
        )
        if (
            old.get(
                "eligible_for_trading_day_returns"
            ) is True
            and not verified
        ):
            replace = False
            print(
                "Preserving existing verified archive"
            )

    if replace:
        path.write_text(
            json.dumps(
                archive,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    paths = sorted(
        p.name
        for p in DEST.glob("????-??-??.json")
    )

    verified_dates = set()

    for name in paths:
        snapshot = json.loads(
            (DEST / name).read_text(
                encoding="utf-8"
            )
        )

        market_date = snapshot.get(
            "verified_market_date"
        )

        if (
            snapshot.get(
                "eligible_for_trading_day_returns"
            ) is True
            and market_date
        ):
            verified_dates.add(market_date)

    index = {
        "note": (
            "Only independently checked TWSE "
            "and TPEx dates count as verified."
        ),
        "snapshots": paths,
        "verified_trading_sessions": len(
            verified_dates
        ),
    }

    INDEX.write_text(
        json.dumps(
            index,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("Archived:", filename)
    print("TWSE verified:", twse_ok)
    print("TPEx verified:", tpex_ok)
    print("Full-market verified:", verified)
    print(
        "Verified sessions:",
        len(verified_dates),
    )


if __name__ == "__main__":
    main()
