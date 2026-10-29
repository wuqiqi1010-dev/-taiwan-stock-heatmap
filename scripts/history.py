
"""Archive official quotes as unverified observation snapshots.

Fetch dates are not necessarily verified trading dates.
Do not calculate trading-day returns without date verification.
"""

import datetime as dt
import hashlib
import json
import pathlib
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data.json"
DEST = ROOT / "history"
INDEX = DEST / "index.json"


def main():
    data = json.loads(SOURCE.read_text(encoding="utf-8"))

    records = data.get("records")
    if not isinstance(records, list) or len(records) < 500:
        raise SystemExit("Refusing to archive incomplete quote data")

    sources = data.get("sources", {})
    if not all(sources.get(m, {}).get("ok") for m in ("TWSE", "TPEx")):
        raise SystemExit("Both exchange feeds must succeed")

    fetched = dt.datetime.fromisoformat(data["fetched_at"])
    if fetched.tzinfo is None:
        raise SystemExit("Missing timezone in fetched_at")

    local = fetched.astimezone(ZoneInfo("Asia/Taipei"))

    # Before market close: skip archiving without failing Actions.
    if local.hour < 16:
        print("Before 16:00 Asia/Taipei: skipping archive")
        return

    # Weekends: no new snapshot.
    if local.weekday() >= 5:
        print("Weekend: skipping archive")
        return

    verified_market_date = (
        data.get("market_date")
        if data.get("date_verified") is True
        else None
    )

    records = sorted(
        records,
        key=lambda r: (r.get("market", ""), r.get("code", ""))
    )

    digest = hashlib.sha256(
        json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True
        ).encode("utf-8")
    ).hexdigest()

    DEST.mkdir(exist_ok=True)

    day = local.date().isoformat()
    path = DEST / (day + ".json")

    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if old.get("quotes_sha256") == digest:
            print("Unchanged archive:", day)
            return

    archive = {
        "observation_date": day,
        "fetched_at": data["fetched_at"],
        "verified_market_date": verified_market_date,
        "eligible_for_trading_day_returns": bool(verified_market_date),
        "quotes_sha256": digest,
        "record_count": len(records),
        "records": records,
    }

    path.write_text(
        json.dumps(
            archive,
            ensure_ascii=False,
            separators=(",", ":")
        ),
        encoding="utf-8"
    )

    paths = sorted(
        p.name for p in DEST.glob("????-??-??.json")
    )

    index = {
        "note": (
            "Observation-date archives, not validated trading sessions "
            "unless verified_market_date is populated."
        ),
        "snapshots": paths,
        "verified_trading_sessions": sum(
            bool(
                json.loads(
                    (DEST / name).read_text(encoding="utf-8")
                ).get("verified_market_date")
            )
            for name in paths
        ),
    }

    INDEX.write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print(
        "Archived:",
        path,
        "Verified trading date:",
        verified_market_date
    )


if __name__ == "__main__":
    main()
