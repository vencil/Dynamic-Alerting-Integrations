---
section: Changed
topic: da-tools
issues: [3982]
created: 2026-11-18T17:00:00+00:00
---
- **`audit_rules_drift --out` 與 `generate_tenant_metadata --output` 依 owner 裁決分類（[#3982](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3982)）**：`audit_rules_drift --out` 是 operator 輸出——原地寫，自己擁有的一般檔寫完 `0600`（以前原子取代、`0644`），別人的檔與 symlink 的目標不改權限；寫到一半失敗留空檔（以前留舊檔）；`--out -` 寫到 stdout（以前建出名為 `-` 的檔），收據行改印在 stderr；沒有讀端的 FIFO 由卡住改為結束碼 `2`；空值在解析時結束碼 `2`。`generate_tenant_metadata --output` 是 artifact——照舊原子取代；`--output -` 與空值結束碼 `2`（要 stdout 就不給 `--output`，要名為 `-` 的檔傳 `./-`）；FIFO、`/dev/null` 等裝置、目錄與 `--output F > F` 在寫入前、`--check` 讀取前就結束碼 `2`（以前 FIFO 與 `--check` 會卡住，`/dev/null` 結束碼 `0`）。`migrate_credentials --manifests-out` 的目錄改經共用的 `ensure_dir(mode=0o700)` 建立，權限與訊息不變。
