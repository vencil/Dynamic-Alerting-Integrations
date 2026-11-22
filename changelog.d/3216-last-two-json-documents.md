---
section: Fixed
topic: da-tools
issues: [3216]
created: 2026-11-22T07:20:00+00:00
---
- **`check-planning-status-sync --json` 有 issue 時把 JSON 寫到 stderr、stdout 是空的（[#3216](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3216)）**：`--json | jq` 正好在有東西可讀的時候什麼都拿不到，結束碼 `0`。現在 `--json` 一律在 stdout 交出恰好一份文件，有沒有 issue 都一樣；在 GitHub Actions 下，`--json` 的 `::warning` 行改寫到 stderr，不混進文件。人類可讀模式不變（issue 的說明仍在 stderr）。stdout 已關閉或寫不進去時，有 issue 的 `--json` 由結束碼 `0`（`--strict` 為 `1`）改為 `2`，與沒有 issue 時相同（GitHub Actions 下原本就是 `2`）。`migrate-to-operator --json` 也改由共用出口編碼與寫出，文件內容不變；stdout 寫不進去時的 traceback 變成一行訊息。⚠️ 讀端提早離開時（例如 `--json | head -n3`），文件介於約 8 KB 與 64 KB 之間的 `migrate-to-operator --json` 結束碼可能由 `0` 變成 `2`——這是共用出口對所有交回文件的工具的既有行為。
