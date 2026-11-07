---
section: Changed
topic: da-tools
issues: [3445]
created: 2026-11-07T01:29:05+00:00
---
- **⚠️ Breaking：輸入檔的內容讀不了時一律 rc 2、一行點名旗標與路徑（ops 工具；[#3445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3445)）**：內容不是 UTF-8、語法壞掉或頂層型別不對，不再是 traceback 或被當成空輸入；`--json` 時 stdout 為空。由 0 改為 2：alert-correlate `--input` 頂層是純量、analyze-gaps `--metric-dictionary` 不是 mapping 與 `--tenant-config` 是清單或純量、lint `--policy` 是 `[]`／`0` 等（空檔仍是沒有覆寫）、shadow-verify `--mapping` 空檔。受檢的對象（例如 lint 的 rule 檔）壞掉仍是 1。內部：檔案輸入改以 `_lib_input.add_input(decode=...)` 宣告、由 `_lib_input.document` 讀。
