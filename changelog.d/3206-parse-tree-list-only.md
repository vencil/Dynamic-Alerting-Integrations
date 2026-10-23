---
section: Added
topic: da-guard
issues: [3206]
created: 2026-10-21T08:32:21+00:00
---
- **`da-guard parse-tree --list-only`：只列檔、不讀也不解析內容（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，ADR-036 §7 步驟 9）**：`--list-only --config-dir <dir>` 走 exporter 同一份走訪，每個檔只開啟再關閉，輸出與整棵樹模式同一種文件（`da-guard.parse-tree/v2`，`mode: "list"`）、同一份 `entries`，只是沒有 `files` 與 `rendered_by`，`problem` 只有不讀內容就判得出的 `read_failure` 與 `not_regular_file`。`--list-only --names <檔案或 ->` 只依名稱分類一份 JSON 字串陣列（例如 ConfigMap 的鍵），同一個名字給兩次以結束碼 2 拒絕。conf.d 的檔名規則在 Go 端也只剩一份（`pkg/confdname`），exporter、tenant-api、da-guard 共用；選檔結果不變。格式見 CLI 參考的 guard 一節。
