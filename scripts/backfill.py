
"""V6.1A: batched TWSE historical backfill.

Historical files are separate from the live heatmap.
Uses TWSE official monthly STOCK_DAY data.
"""
import argparse
import datetime as dt
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

        if close <= 0:
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

    try:
        data = json.loads(
            path.read_text(encoding="utf-8")
        )

        return {
            item["date"]: item["close"]
            for item in data.get("series", [])
        }

    except (
        ValueError,
        KeyError,
        TypeError,
    ):
        return {}


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

    candidates = [
        row
        for row in payload.get("records", [])
        if row.get("market") == "TWSE"
        and row.get("common_stock_candidate")
        and str(row.get("code", "")).isdigit()
        and len(str(row["code"])) == 4
    ]

    # Fixed code ordering prevents turnover
    # ranking changes from shifting batch offsets.
    candidates.sort(key=lambda x: x["code"])

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

            try:
                data = fetch(url)

                if data.get("stat") != "OK":
                    errors.append({
                        "month": str(month),
                        "reason": data.get("stat"),
                    })
                else:
                    for item in data.get("data", []):
                        parsed = parse_close(item)

                        if parsed:
                            day, close = parsed

                            if day <= today.isoformat():
                                points[day] = close

            except Exception as error:
                errors.append({
                    "month": str(month),
                    "reason": str(error),
                })

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
        }

        (DEST / f"{code}.json").write_text(
            json.dumps(
                output,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

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
                "name": data.get("name"),
                "sessions": len(
                    data.get("series", [])
                ),
                "errors": len(
                    data.get("errors", [])
                ),
            })

        except (
            ValueError,
            KeyError,
            TypeError,
        ):
            print("Invalid file:", path.name)

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

    (DEST / "index.json").write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        "Completed. Saved stocks:",
        len(manifest),
        flush=True,
    )


if __name__ == "__main__":
    main()
