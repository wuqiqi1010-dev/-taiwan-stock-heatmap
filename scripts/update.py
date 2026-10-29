
import datetime as dt
import http.client
import json
import pathlib
import re
import socket
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "data.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 TaiwanStockHeatmap/3.0",
    "Accept": "application/json",
    "Accept-Encoding": "identity",
    "Connection": "close",
}

SOURCES = {
    "TWSE": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    "TPEx": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
}


def fetch(url, attempts=5):
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(
                url, headers=HEADERS
            )
            with urllib.request.urlopen(
                request, timeout=90
            ) as response:
                raw = response.read()

            rows = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(rows, list):
                raise ValueError("Expected JSON array")

            print("Download successful:", url, len(rows))
            return rows

        except (
            http.client.IncompleteRead,
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
            ConnectionError,
            OSError,
            json.JSONDecodeError,
            UnicodeError,
        ) as error:
            print(
                f"Download attempt {attempt}/{attempts} failed:",
                repr(error),
                flush=True,
            )

            if attempt == attempts:
                raise

            time.sleep(min(attempt * 5, 20))


def first(row, keys):
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def numeric(value):
    if value is None:
        return None

    s = (
        str(value)
        .strip()
        .replace(",", "")
        .replace("%", "")
        .replace("＋", "+")
        .replace("－", "-")
    )

    try:
        return float(s)
    except ValueError:
        return None


def parse(market, rows):
    result = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        if market == "TWSE":
            code = first(row, ["Code", "證券代號"])
            name = first(row, ["Name", "證券名稱"])
            close = numeric(first(row, [
                "ClosingPrice", "收盤價"
            ]))
            change = numeric(first(row, [
                "Change", "漲跌價差"
            ]))
            turnover = numeric(first(row, [
                "TradeValue", "成交金額"
            ]))
        else:
            code = first(row, [
                "SecuritiesCompanyCode",
                "SecuritiesCode",
                "Code",
                "代號",
                "股票代號",
            ])
            name = first(row, [
                "CompanyName",
                "SecuritiesCompanyName",
                "Name",
                "名稱",
                "股票名稱",
            ])
            close = numeric(first(row, [
                "Close", "ClosingPrice", "收盤"
            ]))
            change = numeric(first(row, [
                "Change", "ChangeAmount", "漲跌"
            ]))
            turnover = numeric(first(row, [
                "TradeValue", "TradingValue", "成交金額"
            ]))

        code = str(code or "").strip()

        if not re.fullmatch(r"\d{4}", code):
            continue

        if close is None or close <= 0:
            continue

        previous = (
            close - change
            if change is not None
            else None
        )

        pct = (
            change / previous * 100
            if previous is not None and previous > 0
            else None
        )

        result.append({
            "code": code,
            "name": str(name or ""),
            "market": market,
            "close": close,
            "change": change,
            "pct": round(pct, 3) if pct is not None else None,
            "turnover": turnover,
            "common_stock_candidate": (
                code[0] in "123456789"
                and not code.startswith("91")
            ),
        })

    return result


def main():
    now = dt.datetime.now(ZoneInfo("Asia/Taipei"))
    records = []
    status = {}

    for market, url in SOURCES.items():
        try:
            rows = fetch(url)
            parsed = parse(market, rows)

            if len(parsed) < 100:
                raise ValueError(
                    f"Too few valid rows: {len(parsed)}"
                )

            records.extend(parsed)

            status[market] = {
                "ok": True,
                "rows": len(parsed),
                "url": url,
            }

        except Exception as error:
            status[market] = {
                "ok": False,
                "error": str(error),
                "url": url,
            }

    if not all(s["ok"] for s in status.values()):
        raise SystemExit(
            "Source validation failed: "
            + json.dumps(status, ensure_ascii=False)
        )

    codes = [
        (r["market"], r["code"])
        for r in records
    ]

    if len(codes) != len(set(codes)):
        raise SystemExit(
            "Duplicate market/code combinations"
        )

    payload = {
        "fetched_at": now.isoformat(),
        "market_date": "尚未由官方來源驗證",
        "date_verified": False,
        "note": (
            "官方行情檢視版。交易日期尚未驗證；"
            "產業分類及歷史報酬尚未接入。"
        ),
        "sources": status,
        "records": records,
    }

    OUT.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    print(
        "Saved",
        len(records),
        "official quotes",
    )


if __name__ == "__main__":
    main()
