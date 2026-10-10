---
section: Added
topic: alertmanager-routing
issues: [2766, 2759]
created: 2026-10-10T01:34:53+00:00
---
- **讀取端差異清單與雙向測試（alertmanager-routing；[#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766)）**：da-guard 與 tenant-api 讀 `_domain_policy.yaml` 與產生器不同的每一列，由 Go 測試寫進快照，再由一支 Python 測試計算方向（Go 寬／嚴／值不同）並對照差異清單；產生器以 `--validate --strict` 會擋、Go 卻放行的檔也算 Go 寬。沒列的差異、已消失的差異、列數或方向不符都會紅。[#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759) 的 A、F、K1、N 四類寬的分歧已加進語料、逐列列為擋住第二階段的項目。`make pre-tag` 新增 `reader-divergence-expiry`：清單有過期記錄時擋 tag（一般 PR CI 只警告）。這是 [ADR-036](docs/adr/036-single-parser-effective-config.md) 實作順序的第二步。
