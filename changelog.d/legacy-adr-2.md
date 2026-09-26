---
section: Changed
topic: adr
issues: [1469, 1516, 1549]
created: 2026-09-26T17:00:00+00:00
---
- **既有 ADR 事實校正（adr）**：ADR-017 的 effective config 段重寫為「規則 → 例外 → 代價」並校正十餘處與實作不符的敘述，明寫把 `_routing_defaults` 縮排進 `defaults:` 會讓沒有自己 `_routing` 的租戶路由靜默消失；ADR-016 更正為 exporter 實際不遞迴掃描子目錄（只有 `/effective` 解析會），保留「應該遞迴」的承諾；ADR-033 的語言政策改為註明例外的寫法。
