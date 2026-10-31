# 歷史回補開發紀錄｜2026-11-01（台灣時間）

## 接續基準與已查閱內容

- Repository: wuqiqi1010-dev/-taiwan-stock-heatmap
- 基準 main: 0d35d2f7d6384084ce3f897681b71fd102426337。
- 已讀 scripts/backfill.py、scripts/history.py 的結構、兩份既有 Actions、歷史樣本、README，以及 scripts/.github 的提交紀錄。
- 66787fd 已將回補擴為近七個月、每批40檔；c143ab5 已建立每小時自動選批。這些工作不重做。
- 每日更新為台灣週一至週五21:00（0 13 * * 1-5），執行 update.py 及 history.py。
- 回補為每小時 UTC 第15分排程；Actions 實際啟動可能排隊。保留既有 cron。

## 寫入權限實測

- get_repo 顯示帳號 push/admin=true，讀取及公開 clone 成功。
- 僅嘗試一次建立 fix/backfill-integrity-20261101，回覆403 Resource not accessible by integration，未建立遠端分支，未修改main。
- 可見 installations 清單為空；此結果不足以判斷實際 App 授權設定。帳號權限與連接器授權是不同層次。
- 停止重連與重試，不改用其他遠端寫入 API 繞測。

## 現況與問題

- 現有1082個股票歷史檔，17個含抓取錯誤，沒有空series。
- 最新 data.json 有1081個TWSE候選；檔案總數不能代表目前候選覆蓋率。
- 最近讀取的6次回補Actions均為success，且同一head SHA；原選批只看檔案存在，錯誤檔不會重試。
- 舊資料缺逐月來源驗證紀錄；新稽核將1081個候選列待驗證。這不是認定1081檔全部資料錯誤。

## 本機已完成

- 共用完整性檢查：股票身分、日期格式/排序/重複/未來日期、有限正價格、空資料、既有errors、目標月份、當月刷新。
- 逐月檢查官方回應stat、月份、股票代號、每列解析；全部通過才取代該月。
- 僅將成交量、成交金額、成交筆數皆為0且OHLC皆為`--`的官方列記入`no_trade_days`及回應列數；不據此推斷停牌。其他缺值或格式異常仍使月份驗證失敗。
- 儲存逐月筆數與SHA256，以辨識已驗證資料後續缺列/修改。
- 不明空月份或停牌/新上市缺資料列待查，不直接宣稱完成。
- 月份失敗時保留原價格資料，記錄錯誤供下次重試。
- 原子寫檔，避免中途損壞；索引列出完整與未完整狀態。
- 最久未嘗試的未完成批次優先，避免固定錯誤阻塞所有後續批次。
- 先提交重試狀態，再驗證批次；不完整時Actions失敗，避免假成功。
- 新增測試分支/PR測試工作流程，不增加定時任務。
- 可用 `python scripts/backfill.py --code 1909 --month 2026-08` 僅重抓指定股票月份；其他月份及股票資料不變。
- 未指定 `--code`/`--month` 時維持原批次行為；每小時歷史回補與每日行情更新排程不變。

## 測試與未驗證範圍

- python -m unittest discover -s tests -v：7項通過（含10種損壞資料子情境）。
- 模擬官方成功回應→完成；再回傳損壞資料→保留原價格、標記失敗、完整檔數歸零。
- 驗證損壞舊檔、日期/價格、批次輪替、原子寫入失敗保留。
- py_compile、git diff --check通過。
- 每日update.yml與基準逐位元一致；每小時cron保留。
- 尚未推送或在GitHub Actions實跑，也未連線TWSE實測新程式。
- 月度來源驗證確認本機忠實保存官方回應；尚無獨立交易日曆/上市停牌名單核對，不能保證官方回應本身沒有漏交易日。
- 未含TPEx歷史、除權息調整、法人/分點資料。不得稱為全市場複盤完成。
- 當月只有在成功且身分/月分正確的官方回應為空、而日期仍早於該月首個平日時，才標記為尚未開始；首個平日後的空回應及任何抓取/API 錯誤仍列為未完整並重試。平日不等於交易日，故此規則只避免月初週末造成的假警報，不代表交易日曆核驗。

## 可執行替代方案（不需重新連結）

使用你的GitHub帳號開啟本專案的Codespaces（Code → Codespaces → Create codespace on main），將交付的 backfill-integrity.patch 上傳至專案根目錄，在終端機執行：

```bash
git switch main
git pull --ff-only
git switch -c fix/backfill-integrity-20261101
git am backfill-integrity.patch
python -m unittest discover -s tests -v
python scripts/backfill_integrity.py --plan
python scripts/backfill_integrity.py > backfill-audit.json
git push -u origin fix/backfill-integrity-20261101
```

若 main 後續有變動造成git am衝突：先git am --abort，保留分支與現況，重新比對差異；不要強制覆蓋main。

推送後在Actions查看「Test historical backfill integrity」，全部通過再開PR。先在測試分支手動執行「V6 Historical Backfill Auto」驗證一批；該工作流程會寫測試分支的歷史檔，保留足夠執行時間，先確認結果再考慮合併。不要將所有回補當作測試成功的先決條件。

## 下一步與下次接手順序

1. 先讀本紀錄，確認遠端是否已出現測試分支/PR及新提交，避免再次重做。
2. 分支測試真實TWSE回應格式、失敗後重試與索引狀態；必要時調整回應欄位驗證。
3. 獨立核對交易日曆、上市日、停牌及無成交情形，才能判斷「合法缺日」。
4. 合併前複核每日更新與每小時回補不衝突；現有兩個workflow的main推送競爭未在本次改動處理。
5. 跑完逐月遷移稽核後，才據實報告完成數；其後再擴展TPEx與輪動複盤。
