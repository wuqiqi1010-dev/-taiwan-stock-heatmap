
"""V6 phase 1: backfill independently dated TWSE monthly closes.

Run manually. Never modifies data.json or existing history snapshots.
"""
import datetime as dt
import json
import pathlib
import time
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data.json"
DEST = ROOT / "historical" / "twse"

MONTHS = 4
MAX_STOCKS = 30


def fetch(url):
    last = None

    for attempt in range(4):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )

            with urllib.request.urlopen(
                req, timeout=35
            ) as response:
                return json.loads(
                    response.read().decode("utf-8-sig")
                )

        except Exception as error:
            last = error
            print(
                "Retry",
                attempt + 1,
                repr(error),
                flush=True,
            )
            time.sleep(3 * (attempt + 1))

    raise RuntimeError(str(last))


def months_back(today, count):
    year = today.year
    month = today.month

    for _ in range(count):
        yield dt.date(year, month, 1)

        month -= 1

        if month == 0:
            year -= 1
            month = 12


def parse_row(row):
    try:
        parts = str(row[0]).strip().split("/")

        if len(parts) != 3:
            return None

        day = dt.date(
            int(parts[0]) + 1911,
            int(parts[1]),
            int(parts[2]),
        )

        close = float(
            str(row[6]).replace(",", "")
        )

        if close <= 0:
            return None

        return {
            "date": day.isoformat(),
            "close": close,
        }

    except (ValueError, IndexError, TypeError):
        return None


def main():
    payload = json.loads(
        SOURCE.read_text(encoding="utf-8")
    )

    candidates = [
        row
        for row in payload.get("records", [])
        if row.get("market") == "TWSE"
        and row.get("common_stock_candidate")
        and str(row.get("code", "")).isdigit()
        and len(str(row.get("code"))) == 4
    ]

    candidates.sort(
        key=lambda row: (
            -(row.get("turnover") or 0),
            str(row.get("code")),
        )
    )

    candidates = candidates[:MAX_STOCKS]

    today = dt.datetime.now(
        ZoneInfo("Asia/Taipei")
    ).date()

    DEST.mkdir(
        parents=True,
        exist_ok=True,
    )

    manifest = []

    for index, row in enumerate(candidates, 1):
        code = str(row["code"])

        points = {}
        errors = []

        for month in months_back(today, MONTHS):
            url = (
                "https://www.twse.com.tw/"
                "exchangeReport/STOCK_DAY"
                f"?response=json"
                f"&date={month:%Y%m%d}"
                f"&stockNo={code}"
            )

            try:
                data = fetch(url)

                if data.get("stat") != "OK":
                    errors.append(
                        f"{month:%Y-%m}: "
                        f"{data.get('stat')}"
                    )
                else:
                    for item in data.get("data", []):
                        parsed = parse_row(item)

                        if (
                            parsed
                            and parsed["date"]
                            <= today.isoformat()
                        ):
                            points[
                                parsed["date"]
                            ] = parsed["close"]

            except Exception as error:
                errors.append(
                    f"{month:%Y-%m}: {error}"
                )

            time.sleep(0.35)

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
        }

        (DEST / f"{code}.json").write_text(
            json.dumps(
                output,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

        manifest.append({
            "code": code,
            "name": row.get("name"),
            "sessions": len(series),
            "errors": len(errors),
        })

        print(
            f"[{index}/{len(candidates)}] "
            f"{code}: {len(series)} sessions, "
            f"{len(errors)} errors",
            flush=True,
        )

    summary = {
        "generated_at": dt.datetime.now(
            ZoneInfo("Asia/Taipei")
        ).isoformat(),
        "coverage": (
            "TWSE top turnover pilot only; "
            "NOT full-market or TPEx"
        ),
        "months_requested": MONTHS,
        "price_type": (
            "unadjusted close; "
            "corporate actions not adjusted"
        ),
        "stocks": manifest,
    }

    (DEST / "index.json").write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        "Backfill completed. "
        "Pilot only. "
        "Existing snapshots unchanged."
    )


if __name__ == "__main__":
    main()
