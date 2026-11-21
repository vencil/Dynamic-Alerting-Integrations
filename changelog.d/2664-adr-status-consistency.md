---
section: Added
topic: docs
issues: [2664]
created: 2026-11-21T12:34:26+00:00
---
- **ADR 狀態三處不一致時 pre-commit 擋下（docs；[#2664](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2664)）**：一份 ADR 的狀態寫在 frontmatter `status:`（planning-index 讀它）、正文 `## 狀態` 那一行（ADR 索引表讀它）與英文版 `## Status` 那一行（沒有程式讀它）。過去只改其中一處，兩張索引就會顯示不同狀態而沒有閘門擋。`scripts/dx/generate_adr_index.py` 的 `--check` 與 `--write` 現在先比對這三處（狀態名正規化後比較，不比 emoji），缺 frontmatter `status:` 或英文版讀不到狀態行也算不一致，一律 rc 1 且不寫檔；pre-commit 的 `adr-index-check` 因此一併守住。
