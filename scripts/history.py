
"""Archive Taiwan stock quotes with independent date checks."""

import datetime as dt
import hashlib
import json
import pathlib
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


def number(value):
    try:
        return float(str(value).strip().replace(",", ""))
    except (ValueError, TypeError):
        return None


def roc_date(value):
    s = str(value or "").strip().replace("/", "")
    if len(s) != 7 or not s.isdigit():
        return None
    try:
        return dt.date(
            int(s[:3]) + 1911,
            int(s[3:5]),
            int(s[5:7]),
        )
    except ValueError:
        return None


def fetch_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def verify_twse(records, day):
    lookup = {
        str(r["code"]): r
        for r in records
        if r.get("market") == "TWSE"
    }

    checks = {}

    for code in ("2330", "2317"):
        record = lookup.get(code)
        if record is None:
            checks[code] = {"matched": False}
            continue

        url = (
            "https://www.twse.com.tw/exchangeReport/"
            f"STOCK_DAY?response=json&date={day:%Y%m%d}"
            f"&stockNo={code}"
        )

        try:
            data = fetch_json(url)
            target = (
                f"{day.year - 1911}/"
                f"{day.month:02d}/{day.day:02d}"
            )
            official = None

            if data.get("stat") == "OK":
                for row in data.get("data", []):
                    if (
                        isinstance(row, list)
                        and len(row) > 6
                        and str(row[0]).strip() == target
                    ):
                        official = number(row[6])
                        break

            observed = number(record.get("close"))
            matched = (
                official is not None
                and observed is not None
                and abs(official - observed) < 0.011
            )

            checks[code] = {
                "official_close": official,
                "observed_close": observed,
                "matched": matched,
            }

        except Exception as error:
            checks[code] = {
                "matched": False,
                "error": str(error),
            }

    return (
        len(checks) == 2
        and all(x["matched"] for x in checks.values()),
        checks,
    )


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
            raise ValueError("TPEx response is not a list")

        official = {}

        for row in rows:
            if not isinstance(row, dict):
                continue

            code = str(
                row.get("SecuritiesCompanyCode")
                or row.get("SecuritiesCode")
                or ""
            ).strip()

            if code in ("8086", "8069"):
                official[code] = row

        for code in ("8086", "8069"):
            row = official.get(code)
            record = lookup.get(code)

            if row is None or record is None:
                checks[code] = {"matched": False}
                continue

            official_day = roc_date(row.get("Date"))
            official_close = number(row.get("Close"))
            observed_close = number(record.get("close"))

            matched = (
                official_day == day
                and official_close is not None
                and observed_close is not None
                and abs(official_close - observed_close) < 0.011
            )

            checks[code] = {
                "official_date": (
                    official_day.isoformat()
                    if official_day else None
                ),
                "official_close": official_close,
                "observed_close": observed_close,
                "matched": matched,
            }

    except Exception as error:
        checks["error"] = str(error)

    return (
        all(
            checks.get(code, {}).get("matched") is True
            for code in ("8086", "8069")
        ),
        checks,
    )


def main():
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    records = data.get("records")

    if not isinstance(records, list) or len(records) < 500:
        raise SystemExit("Incomplete quote data")

    sources = data.get("sources", {})

    if not all(
        sources.get(market, {}).get("ok")
        for market in ("TWSE", "TPEx")
    ):
        raise SystemExit("Exchange data incomplete")

    fetched = dt.datetime.fromisoformat(data["fetched_at"])

    if fetched.tzinfo is None:
        raise SystemExit("Missing fetch timezone")

    local = fetched.astimezone(TZ)

    if local.hour < 16 or local.weekday() >= 5:
        print("Outside archive window; skipping")
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

    twse_ok, twse_checks = verify_twse(records, day)
    tpex_ok, tpex_checks = verify_tpex(records, day)

    fully_verified = twse_ok and tpex_ok

    DEST.mkdir(exist_ok=True)

    filename = day.isoformat() + ".json"
    path = DEST / filename

    archive = {
        "observation_date": day.isoformat(),
        "fetched_at": data["fetched_at"],
        "verified_market_date": (
            day.isoformat() if fully_verified else None
        ),
        "eligible_for_trading_day_returns": fully_verified,
        "twse_reference_date_verified": twse_ok,
        "twse_reference_checks": twse_checks,
        "tpex_date_verified": tpex_ok,
        "tpex_reference_checks": tpex_checks,
        "verification_note": (
            "Both markets independently checked against "
            "official sources using reference stock prices "
            "and the applicable trading date."
            if fully_verified
            else "Date verification incomplete. Not eligible "
                 "for full-market trading-day returns."
        ),
        "quotes_sha256": digest,
        "record_count": len(records),
        "records": records,
    }

    # Preserve an existing verified archive if a later
    # request fails verification.
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if (
            old.get("eligible_for_trading_day_returns") is True
            and not fully_verified
        ):
            print("Keeping previously verified archive")
        else:
            path.write_text(
                json.dumps(
                    archive,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
    else:
        path.write_text(
            json.dumps(
                archive,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    paths = sorted(
        p.name for p in DEST.glob("????-??-??.json")
    )

    verified_count = 0

    for name in paths:
        snapshot = json.loads(
            (DEST / name).read_text(encoding="utf-8")
        )
        if (
            snapshot.get("eligible_for_trading_day_returns") is True
            and snapshot.get("verified_market_date")
        ):
            verified_count += 1

    index = {
        "note": (
            "Trading sessions counted only when both "
            "markets pass independent date checks."
        ),
        "snapshots": paths,
        "verified_trading_sessions": verified_count,
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
    print("Full-market verified:", fully_verified)
    print("Verified sessions:", verified_count)
    print(
        "TPEx checks:",
        json.dumps(tpex_checks, ensure_ascii=False),
    )


if __name__ == "__main__":
    main()
