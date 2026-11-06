---
section: Fixed
topic: exporter
issues: [3323]
created: 2026-11-05T23:55:53+00:00
---
- **同一棵樹的 served-values 每次輸出相同（exporter／da-guard；[#3323](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3323)）**：非閾值形狀的映射或清單（例如 `_routing`、`_custom_alerts`、未登記的 `_` 鍵）重新序列化時，若有兩個以上的時間戳記鍵（`2026-12-31T00:00:00Z:`），先前這些鍵的順序每次執行可能不同，`unserved` 等欄位的字串因此不可重現。現在時間戳記鍵依鍵的 YAML 文字排序，其他鍵的順序與寫法不變。
