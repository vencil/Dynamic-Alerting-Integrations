---
section: Changed
topic: ci
issues: []
created: 2026-11-04T16:41:20+00:00
---
- **⚠️ BREAKING（branch protection）：Go 的 required check 由 `Go Tests (1.26)` 改為 `Go Tests`**：Go 版本只寫在各 module 的 `go.mod`，CI 的 job、coverage artifact 與 cache key 名稱不再帶版本，與 `Python Tests` 同一個做法。新守衛擋下任何帶版本號的 required check 名稱。
