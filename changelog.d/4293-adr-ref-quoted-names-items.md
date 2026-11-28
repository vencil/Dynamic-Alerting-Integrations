---
section: Fixed
topic: lint
issues: [4293]
created: 2026-11-28T12:00:00+00:00
---
- **ADR 小節引用檢查也核對引號裡的段名、項目編號與 ADR-024 舊代號（lint；[#4293](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4293)）**：`check_adr_section_refs` 在 `ADR-NNN §小節` 後面緊接引號裡的名稱時，要求這段文字出現在該小節（中英任一份）；緊接 `item N`／`第 N 項` 時，要求該小節有這個編號的頂層項目；`§2b` 這類帶字母的小節編號不再被當成 `§2` 放行；`ADR-024` 後接它分批上線與被取代草稿的代號（`OQ-6`、`PR3b`、`AC-7`、`Phase 1` 等）一律失敗。既有的三處失效段名與 ADR-024 舊代號已改指現行小節，印給 operator 的訊息一併更正。
