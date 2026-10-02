---
section: Fixed
topic: dev-workflow
issues: [2558]
created: 2026-10-02T21:25:39+08:00
---
- **`da-tools init` 在 Windows 上列出的檔案路徑改用 `/`（da-tools；[#2558](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2558)）**：`--dry-run` 的「會被產生」清單與實跑結尾的 `✓` 清單以前印成 `conf.d\db-a.yaml`，同一次輸出裡的其他說明卻寫 `conf.d/db-a.yaml`，一棵樹出現兩種寫法。現在兩份清單在每個平台印出相同的文字；Linux 與 macOS 上的輸出不變。
