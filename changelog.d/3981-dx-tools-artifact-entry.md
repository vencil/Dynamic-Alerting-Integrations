---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-25T00:58:40+00:00
---
- **四支會改寫 repo 檔的工具改經共用的寫入入口（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：`_lib_exporter_presence --write`、`scaffold_lint`、`renovate_fixup`、`migrate_ssot_language --execute`。寫入改經 `write_repo_artifact`：原子取代，hard link 或別人的檔改為就地寫入並在 stderr 多一行 WARN；權限照 checkout（`0644`，可執行檔 `0755`；原本在 `umask 077` 下新檔是 `0600`）。寫入失敗原本是 traceback 加 `unexpected internal error`，現在是結束碼 `2` 加一行。所有目標在第一次寫入前判斷：以 `>>` 把 stdout 附加到某份目標（`scaffold_lint` 的 config 用 `>` 亦同）原本是結束碼 `0`，照寫、報告接在後面，現在是 `2`、不寫任何檔（`migrate` 也不做任何 rename）；`scaffold_lint --force` 的目標是目錄、`renovate_fixup` 的目標被拒時，原本已寫出前面的檔，現在不寫。讀到 FIFO（rule pack，含不帶 `--write` 的檢查；`scaffold_lint` 的 config；`renovate_fixup` 讀的檔）原本卡住，現在是 `2`；非 UTF-8 的 traceback 改成一行。`_lib_exporter_presence --write` 只寫內容有變的 pack（原本每份都重寫）。
