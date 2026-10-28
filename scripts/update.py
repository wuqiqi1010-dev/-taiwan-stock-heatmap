import json, urllib.request, datetime, pathlib, re
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "data.json"
HEADERS = {"User-Agent": "TaiwanSectorHeatmap/1.0", "Accept": "application/json"}
SOURCES = {
    "TWSE": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    "TPEx": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
}

def get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=40) as response:
        return json.load(response)

def number(value):
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return None
    value = str(value).strip().replace(",", "").replace("%", "").replace("＋", "+").replace("－", "-")
    try:
        return float(value)
    except ValueError:
        return None

def pick(row, names):
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None

def parse_market(market, rows):
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if market == "TWSE":
            code = pick(row, ["Code", "證券代號"])
            name = pick(row, ["Name", "證券名稱"])
            close = number(pick(row, ["ClosingPrice", "收盤價"]))
            change = number(pick(row, ["Change", "漲跌價差"]))
            turnover = number(pick(row, ["TradeValue", "成交金額"]))
        else:
            code = pick(row, ["SecuritiesCompanyCode", "SecuritiesCode", "Code", "代號", "股票代號"])
            name = pick(row, ["CompanyName", "SecuritiesCompanyName", "Name", "名稱", "股票名稱"])
            close = number(pick(row, ["Close", "ClosingPrice", "收盤"]))
            change = number(pick(row, ["Change", "ChangeAmount", "漲跌"]))
            turnover = number(pick(row, ["TradeValue", "TradingValue", "成交金額"]))
        code = str(code or "").strip()
        if not re.fullmatch(r"\d{4}", code) or close is None or close <= 0:
            continue
        previous = close - change if change is not None else None
        pct = change / previous * 100 if previous and previous > 0 else None
        result.append({
            "code": code, "name": str(name or ""), "market": market,
            "close": close, "change": change,
            "pct": round(pct, 3) if pct is not None else None,
            "turnover": turnover
        })
    return result

def main():
    now = datetime.datetime.now(ZoneInfo("Asia/Taipei"))
    records = []
    status = {}
    for market, url in SOURCES.items():
        try:
            raw = get(url)
            if not isinstance(raw, list):
                raise ValueError("Expected JSON array")
            parsed = parse_market(market, raw)
            if len(parsed) < 100:
                raise ValueError("Too few valid rows: " + str(len(parsed)))
            records.extend(parsed)
            status[market] = {"ok": True, "rows": len(parsed), "url": url}
        except Exception as error:
            status[market] = {"ok": False, "error": str(error), "url": url}
    if not all(item["ok"] for item in status.values()):
        print(json.dumps(status, ensure_ascii=False))
        raise SystemExit("Official source failed; previous data preserved")
    payload = {
        "fetched_at": now.isoformat(),
        "market_date": "尚未由官方來源驗證",
        "date_verified": False,
        "note": "官方個股行情原型；產業分類、歷史報酬與法人流向尚未接入。",
        "sources": status,
        "records": records
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("Saved", len(records), "records")

if __name__ == "__main__":
    main()
