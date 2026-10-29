
"""Archive quotes and cross-check TWSE trading date.

TWSE verification is partial. TPEx remains unverified.
Never claim full-market verification from TWSE alone.
"""

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


def official_twse_close(code, day):
    """Read the specified date's close from TWSE STOCK_DAY."""
    url = (
        "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
        f"?response=json&date={day:%Y%m%d}&stockNo={code}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(request, timeout=25) as response:
        data = json.load(response)

    if data.get("stat") != "OK":
        return None

    target = f"{day.year - 1911}/{day.month:02d}/{day.day:02d}"

    for row in data.get("data", []):
        if not isinstance(row, list) or len(row) < 7:
            continue

        if str(row[0]).strip() != target:
            continue

        try:
            return float(str(row[6]).replace(",", ""))
        except ValueError:
            return None

    return None


def verify_twse(records, day):
    """Require two TWSE reference stocks to match official closes."""
    lookup = {
        (str(r.get("market")), str(r.get("code"))): r
        for r in records
    }

    checked = {}

    for code in ("2330", "2317"):
        record = lookup.get(("TWSE", code))

        if record is None:
            return False, checked

        try:
            official = official_twse_close(code, day)
            observed = float(record["close"])

            matched = (
                official is not None
                and abs(official - observed) < 0.011
            )

            checked[code] = {
                "official_close": official,
                "observed_close": observed,
                "matched": matched,
            }

        except Exception as error:
            checked[code] = {
                "matched": False,
                "error": str(error),
            }

    return all(
        x.get("matched") is True for x in checked.values()
    ), checked


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
        raise SystemExit("Exchange download incomplete")

    fetched = dt.datetime.fromisoformat(data["fetched_at"])

    if fetched.tzinfo is None:
        raise SystemExit("Missing fetched_at timezone")

    local = fetched.astimezone(TZ)

    if local.hour < 16:
        print("Before 16:00 Taiwan time; archive skipped")
        return

    if local.weekday() >= 5:
        print("Weekend; archive skipped")
        return

    day = local.date()

    records = sorted(
        records,
        key=lambda r: (r.get("market", ""), r.get("code", "")),
    )

    digest = hashlib.sha256(
        json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    twse_verified, checks = verify_twse(records, day)

    print(
        "TWSE date cross-check:",
        json.dumps(checks, ensure_ascii=False),
    )

    # A TWSE cross-check cannot verify TPEx data.
    # Keep full-market eligibility disabled.
    fully_verified = False

    DEST.mkdir(exist_ok=True)

    filename = day.isoformat() + ".json"
    path = DEST / filename

    archive = {
        "observation_date": day.isoformat(),
        "fetched_at": data["fetched_at"],
        "verified_market_date": None,
        "eligible_for_trading_day_returns": fully_verified,
        "twse_reference_date_verified": twse_verified,
        "twse_reference_checks": checks,
        "tpex_date_verified": False,
        "verification_note": (
            "TWSE reference closes checked against official "
            "STOCK_DAY. TPEx not yet independently verified. "
            "Not eligible for full-market trading-day returns."
        ),
        "quotes_sha256": digest,
        "record_count": len(records),
        "records": records,
    }

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
            "Observation snapshots. Full-market trading dates "
            "are not yet verified."
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
    print("TWSE reference verified:", twse_verified)
    print("TPEx verified: False")
    print("Full-market verification: False")


if __name__ == "__main__":
    main()
