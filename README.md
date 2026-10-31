# 台股市場雷達

## 歷史回補

- TWSE 與 TPEx 的個股日收盤資料分別存放於 `historical/twse/` 與
  `historical/tpex/`，各自維護 `index.json`；TPEx 股票清單另存於
  `historical/tpex/stocks.json`。
- TPEx 回補使用櫃買中心官方「個股日成交資訊」逐月查詢，
  每月檢查 API 狀態、月份與股票身分、逐日日期/收盤價、重複或排序異常，
  並記錄實際報價筆數及 SHA-256。已驗證的過去月份保留不重抓；
  當月持續更新。失敗月份保留原行情並列入重試。
- `backfill-history.yml` 保留 TWSE 每小時工作；
  `backfill-tpex-history.yml` 獨立執行 TPEx 工作。兩者共用不取消的
  GitHub Actions concurrency group，避免同時推送互相覆蓋。
- 本地執行測試：

  ```sh
  python -m unittest discover -s tests -v
  python scripts/backfill_integrity.py --market TPEx
  ```

- 進度索引只驗證官方回傳的報價資料和日期格式/平日，不代表已獨立核對
  交易所完整交易日曆、停牌或未上市日期；資料使用未還原收盤價，未調整除權息。

## 網站

- `index.html` 即時行情來自 `data.json`，含上市及上櫃來源。
- `rotation.html` 共用題材名單，提供按題材、市場和日期複盤；
  只將逐月驗證成功月份的報價納入個股 1/5/20/60 報酬。
- 題材是自訂觀察名單，非交易所正式產業分類；複盤報酬按個股有報價的交易日計算。

官方 TPEx 月資料來源：
<https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock>