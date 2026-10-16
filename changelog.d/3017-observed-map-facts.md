---
section: Changed
topic: threshold-governance
issues: [3017]
created: 2026-10-16T02:18:15+00:00
---
- **observed-map 改記事實，推薦政策只留在 `recommendation_mode`（[#3017](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3017)）**：每個 key 的 alert 比的是哪條序列、往哪個方向比，下界 key 也一併記下（`kafka_active_controllers`、`kafka_broker_count`、`rabbitmq_consumers` 由 `candidates` 改為 `observed_series`），讓回測等其他工具讀得到；推薦結果不變。比較方向是 `<` 卻沒分類、或 `LOWER_BOUND_RESOLVED` 的 pick 已不是 alert 比的序列時，drift-guard 改為失敗（先前是靜默的 needs_review）。
