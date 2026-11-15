---
section: Added
topic: ci
issues: [3946]
created: 2026-11-15T00:30:00+00:00
---
- **workflow 的 `run:` step 不得讀一個只會是 0 的 `$?`（ci；[#3946](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3946)）**：沒寫 `shell:` 的 step 是 `bash -e`，`指令` 換行 `rc=$?` 在指令失敗時到不了那一行，後面用來回報失敗的摘要、step output 與 `::error::` 全部不會執行。`tests/ops/test_workflow_status_reads.py` 對每個提到 `$?` 的 step 實際執行腳本（指令一律跳過、寫入限制在暫存目錄內），再逐一讓每個指令失敗，由 bash 自己決定 errexit 的結果；讀不到非零的、走不到的、判不了的都算失敗。寫法是先 `rc=0` 再 `指令 || rc=$?`。⚠️ 需要 Linux 的 Landlock；沒有時在 CI 是失敗、本機是 skip。
