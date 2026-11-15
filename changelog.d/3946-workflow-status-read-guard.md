---
section: Added
topic: ci
issues: [3946]
created: 2026-11-15T00:30:00+00:00
---
- **workflow 與 composite action 的 `run:` 裡，`$?` 只准一種寫法（ci；[#3946](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3946)）**：沒寫 `shell:` 的 step 是 `bash -e`，`指令` 換行 `rc=$?` 在指令失敗時到不了那一行，後面用來回報失敗的摘要、step output 與 `::error::` 全部不會出現。`tests/ops/test_workflow_status_reads.py` 以純文字規則要求 `$?`／`${?}` 只出現在同一邏輯行結尾的 `指令 || 名稱=$?`、`|| exit $?`、`|| return $?`——這個形狀不論 errexit 開或關都正確。其餘出現一律失敗（含字串、`$(…)`、heredoc、行尾註解）；規則之前就有的裸讀取列在該檔的 `_BARE_READS`，只准縮短。讀不了的 workflow 或 action 檔、非 sh 系 `shell:` 底下的 `$?` 也是失敗。
