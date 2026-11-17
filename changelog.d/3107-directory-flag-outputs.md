---
section: Changed
topic: da-tools
issues: [3107]
created: 2026-11-15T10:30:00+00:00
---
- **八支工具寫進輸出目錄的檔案改走 operator 輸出；目錄旗標給 `-` 改為結束碼 2（da-tools；[#3107](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3107)）**：`validate`、`init`、`operator-generate`、`migrate-to-operator`、`rule-pack-split`、`scaffold`、`migrate`、`onboard` 在 `--output-dir` 之下寫的檔（conf.d 檔除外，仍由 `da-guard write` 寫）交給 kernel 開檔，內容位元組不變，權限一律 `0600`。⚠️ 行為變更：(1) 目錄旗標給 `-` 時 `0` → `2`，argparse 點名旗標，不再建出名為 `-` 的目錄；(2) `rule-pack-split` 的 `validation-report.json` 由 `0644` 改為 `0600`；(3) 既有輸出檔屬於別的使用者而可寫時 `2` → `0`（以前內容寫入後 chmod 失敗、其後的檔不寫），權限不動；(4) 既有輸出檔是 symlink、或 root 寫別人的檔時，目標不再被改成 `0600`；(5) 既有路徑是沒有 reader 的 FIFO 時由卡住改為 `2`；(6) 輸出檔正是本次執行的 stdout（`> 該檔`）時內容改經 stdout 寫入，先前印出的文字不再被截掉，權限不動。
