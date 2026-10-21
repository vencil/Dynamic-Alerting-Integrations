---
section: Changed
topic: da-guard
issues: [3206]
created: 2026-10-21T16:00:00+00:00
---
- **`da-guard parse-tree` 每種模式輸出同一份逐路徑索引，路徑是不是設定檔由 Go 判定（`da-guard.parse-tree/v2`；[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，ADR-036 §2、§7 步驟 9）**：整棵樹、單檔、只列檔、名稱四種模式都輸出 `entries`，每筆寫出全部欄位（`path`、`depth`、`class`、`tenant_named`、`defaults_document`、`readers`、`not_read`、`role`、`problem`，沒有值的是 `null`）；頂層的 `selected`、`unreadable`、`not_read` 改由 `entries` 表達，`da-guard.parse-tree-list/v1` 刪除。`role` 是 `pkg/config` 對這個路徑的判定：`config`（設定檔，讀不了時 `problem` 寫原因）、`dir`（走訪走進去的目錄，每個都列出；名稱像設定檔的目錄另帶 `problem`）、`not_a_file`（碰不到一般檔、也不走進去：指向目錄的 symlink、特殊檔、懸空或迴圈連結、超過 40 層的連結鏈）、`unlistable_dir`（列不出內容的目錄）；只有這些情況是 `not_a_file`，權限不足、EIO、名稱過長與其他失敗一律是 `config`。Python 的讀取端 `_lib_parse_tree` 改讀 v2，遇到 v1 或少欄位的文件整份拒收並提示 da-guard 比工具舊；既有工具的行為不變。兩種格式都還沒發布，對已發布版本不是 breaking。
