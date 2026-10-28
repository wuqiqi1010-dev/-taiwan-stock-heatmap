import datetime as dt
import json
import pathlib
import re
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "data.json"
HEADERS = {"User-Agent": "Mozilla/5.0 TaiwanStockHeatmap/2.0", "Accept": "application/json"}
SOURCES = {
    "TWSE": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    "TPEx": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
}

def fetch(url):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)

def first(row, keys):
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None

def numeric(value):
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("%", "").replace("＋", "+").replace("－", "-")
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
            close = numeric(first(row, ["ClosingPrice", "收盤價"]))
            change = numeric(first(row, ["Change", "漲跌價差"]))
            turnover = numeric(first(row, ["TradeValue", "成交金額"]))
        else:
            code = first(row, ["SecuritiesCompanyCode", "SecuritiesCode", "Code", "代號", "股票代號"])
            name = first(row, ["CompanyName", "SecuritiesCompanyName", "Name", "名稱", "股票名稱"])
            close = numeric(first(row, ["Close", "ClosingPrice", "收盤"]))
            change = numeric(first(row, ["Change", "ChangeAmount", "漲跌"]))
            turnover = numeric(first(row, ["TradeValue", "TradingValue", "成交金額"]))
        code = str(code or "").strip()
        if not re.fullmatch(r"\d{4}", code) or not close or close <= 0:
            continue
        is_common_stock_candidate = code[0] in "123456789" and not code.startswith("91")
        previous = close - change if change is not None else None
        pct = change / previous * 100 if previous and previous > 0 else None
        result.append({
            "code": code, "name": str(name or ""), "market": market,
            "close": close, "change": change,
            "pct": round(pct, 3) if pct is not None else None,
            "turnover": turnover,
            "common_stock_candidate": is_common_stock_candidate,
        })
    return result

def main():
    now = dt.datetime.now(ZoneInfo("Asia/Taipei"))
    records = []
    status = {}
    for market, url in SOURCES.items():
        try:
            rows = fetch(url)
            if not isinstance(rows, list):
                raise ValueError("Expected JSON array")
            parsed = parse(market, rows)
            if len(parsed) < 100:
                raise ValueError(f"Too few valid rows: {len(parsed)}")
            records.extend(parsed)
            status[market] = {"ok": True, "rows": len(parsed), "url": url}
        except Exception as error:
            status[market] = {"ok": False, "error": str(error), "url": url}
    if not all(item["ok"] for item in status.values()):
        raise SystemExit("Source validation failed: " + json.dumps(status, ensure_ascii=False))
    codes = [(r["market"], r["code"]) for r in records]
    if len(codes) != len(set(codes)):
        raise SystemExit("Duplicate market/code combinations: refusing to overwrite data")
    payload = {
        "fetched_at": now.isoformat(),
        "market_date": "尚未由官方來源驗證",
        "date_verified": False,
        "note": "官方行情檢視版；common_stock_candidate 為初步代號篩選，非正式證券類別驗證。產業分類、歷史報酬及法人流向尚未接入。",
        "sources": status,
        "records": records,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("Saved", len(records), "quotes; stock candidates:", sum(r["common_stock_candidate"] for r in records))

if __name__ == "__main__":
    main()
