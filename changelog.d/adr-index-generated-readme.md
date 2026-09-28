---
section: Changed
topic: docs
issues: []
created: 2026-09-28T05:05:19+00:00
---
- **ADR 總覽頁的索引改為自動產生**：`docs/adr/README{,.en}.md` 原本手寫的索引只列到 ADR-024，而且有兩篇的狀態與 ADR 本文不一致；現在和架構文件共用 `make adr-index` 產生的表，列出全部 ADR，過期時 pre-commit 會擋。逐篇手寫摘要已移除，背景與取捨請直接讀各 ADR。
