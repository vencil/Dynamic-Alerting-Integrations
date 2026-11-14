---
section: Fixed
topic: tools
issues: [3797]
created: 2026-11-14T12:20:57+00:00
---
- **⚠️ 檔案輸入一律只判定、只讀一次（tools；[#3797](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3797)）**：`--policy`、`--policy-dsl`、`--base-config`、`--tenant-config`、`--secret-mounts`、`--assert`、`--state`、`onboard --alertmanager-config`／`--scrape-config` 改由宣告讀取，讀完不再回頭碰檔案。修掉判定後檔案消失時的誤判：evaluate-policy 讀成「沒有規則」rc 0、validate-config `--policy-dsl` 讀成沒有規則或 traceback、analyze-gaps 與 state-reconcile 的 traceback。行為變更：`validate-config --policy` 有問題時在任何列執行前結束碼 2、`--json` 下 stdout 為空；`--state` 不再收目錄或 `<(...)`（改用 `-`）；給 `''` 一律結束碼 2，不再當成沒給。
