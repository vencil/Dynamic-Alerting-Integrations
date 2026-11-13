---
section: Changed
topic: dx
issues: [1040]
created: 2026-11-13T11:06:28+00:00
---
- **ADR 的檢查清單改由原始碼標記產生（ADR-027 §9；[#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)）**：ADR-027 §9 的每個子句有固定編號（例如 `3b`），實作子句的測試或 CI 步驟在上一行標記 `adr-check: ADR-027/9.3b`。「實作位置」由 `scripts/tools/dx/gen_adr_checks.py --write` 依標記產生，沒有標記的子句列為尚無檢查；pre-commit 與 CI 的 `adr-checks-drift` 在 ADR 與標記不一致、或標記指向不存在的子句時失敗。清單改由產生，不再需要手動維護。
