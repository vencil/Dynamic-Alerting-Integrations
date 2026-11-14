---
section: Changed
topic: da-tools
issues: [3724]
created: 2026-11-12T20:09:56+00:00
---
- **⚠️ Breaking：已宣告的輸入在判定之後才變動，一律是結束碼 2、點名旗標與路徑（ops 工具；[#3724](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3724)）**：輸入目錄或檔案被刪、目錄下被選中的檔消失，不再用比給定更少的輸入作答。先前 generate-routes 與 explain-route 印出沒有租戶的結果（rc 0）；config-diff 少掉 inhibit 變更；validate-config 把相關的列降成「未檢查」；deprecate 略過讀不到的載體；lint `--rule-pack-dir` 記成一條 ERROR（rc 0）；drift-detect、migrate-credentials crash 或交出降級的計畫；grafana-import `--dashboard-dir` 以點錯旗標的部分答案結束、前面的 dashboard 已 apply（現在先讀完才 apply）；state-reconcile 列為無法解決的 drift（rc 1；選到的 state 檔或 lint `--rule-pack-dir` 的 pack 讀不到，不論原因都改為 2）。由新的故障注入閘門守住，涵蓋範圍見 cli-reference。
