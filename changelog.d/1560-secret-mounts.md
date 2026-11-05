---
section: Added
topic: tools
issues: [1560]
created: 2026-11-05T16:08:40+00:00
---
- **要掛載的 Secret 改由產生器列出、由 operator-check 比對（[#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560)）**：`generate-routes --output-alertmanager`／`--output-configmap` 加 `--secret-mounts-out <檔>`，從產出的設定本身（含 base 的 `*_file` 與頂層 `templates`）寫出要掛載的 Secret 名稱與 key（JSON `da-tools.secret-mounts/v1` 的 `mounts[]`，不含值）；`--validate`／`--apply`／`--dry-run` 帶它結束碼 2。⚠️ 這次執行的任兩個輸出（`-o`、`--findings-json`、`--secret-mounts-out`）或輸出與 `--base-config` 指向同一個檔（含 symlink、hard link）一律結束碼 2、都不寫出，`-o` 與 `--findings-json` 同檔是既有組合的新拒收。`operator-check --secret-mounts <檔>` 比對 Alertmanager CR 的 `spec.secrets`：漏列 FAIL、多列 `da-` 開頭的 WARN，只讀 CR 不讀 Secret；不給則 SKIP。平台 rule pack 新增 `AlertmanagerNotificationsFailing`：webhook 以外的 integration，各失敗原因與 receiver 加總後 15m 失敗率 > 1%。
