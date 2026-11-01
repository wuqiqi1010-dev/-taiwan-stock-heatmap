# 台股市場雷達

## 歷史回補

- TWSE 與 TPEx 的個股日收盤資料分別存放於 `historical/twse/` 與
  `historical/tpex/`，各自維護 `index.json`；TPEx 股票清單另存於
  `historical/tpex/stocks.json`。
- TPEx 回補使用櫃買中心官方「個股日成交資訊」逐月查詢，
  每月檢查 API 狀態、月份與股票身分、官方 `totalCount` 與回傳列數、
  並將報價、無價格、無成交日期集合與官方 `tradingIndex` 月交易日逐日比對；
  同時檢查重複/排序異常並記錄原始列數及 SHA-256。
  已驗證的過去月份保留不重抓；當月持續更新。失敗月份保留原行情並列入重試。
- `backfill-history.yml` 保留 TWSE 每小時工作；
  `backfill-tpex-history.yml` 獨立執行 TPEx 工作。兩者共用不取消的
  GitHub Actions concurrency group，避免同時推送互相覆蓋。
- 本地執行測試：

  ```sh
  python -m unittest discover -s tests -v
  python scripts/backfill_integrity.py --market TPEx
  ```

- TPEx 驗證月份會獨立比對官方日交易指數的交易日；TWSE 進度驗證仍只核對
  官方月資料列。兩者皆不代表停牌或個股未上市日已另行查證；資料使用未還原
  收盤價，未調整除權息。
- TPEx 將官方 9 欄列中的正價收盤寫入序列，並以官方 `--` 列保留無收盤價
  成交／無成交日證據；官方掛牌首日 `*` 可證明掛牌前月份。沒有此證據的空月
  份仍會失敗並重試，不會以空資料宣告完整。

## 網站

- `index.html` 即時行情來自 `data.json`，含上市及上櫃來源。
- `rotation.html` 共用題材名單，提供按題材、市場和日期複盤；
  只將逐月驗證成功月份的報價納入個股 1/5/20/60 報酬。
- 題材是自訂觀察名單，非交易所正式產業分類；複盤報酬按個股有報價的交易日計算。

官方 TPEx 月資料來源：
<https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock>

官方 TPEx 月交易日來源：
<https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingIndex>
