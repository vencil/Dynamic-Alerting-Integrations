---
section: Changed
topic: rule-packs
issues: [3658]
created: 2026-11-07T12:56:25+00:00
---
- **⚠️ Breaking：critical 層的宣告與規則由閘門綁成一份契約；MariaDB 三組 `metric_group` 改名（rule-packs；[#3658](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3658)）**：新的 `critical-tier-twins-check` 要求 registry 宣告的每個 `<base>_critical` 恰有一條 critical 告警，且它是 warning 告警的孿生（只差 critical 那一層；labels 只差 severity，`metric_group` 等於門檻鍵名，`for`、annotation 鍵、群組一致）；讀 critical 層卻沒宣告的規則同樣擋下。MariaDB 的 `metric_group` 由 `connections`／`threads_running`／`replication_lag` 改為 `mysql_connections`／`mysql_threads_running`／`mysql_replication_lag`，依舊值寫的 Alertmanager 路由、silence 或 matcher，升級前先改成同時匹配新舊值（例如 `metric_group=~"connections|mysql_connections"`），升級後再收回。Kubernetes pack 宣告 `container_cpu_critical`（95）、`container_memory_critical`（95）、`container_cpu_throttle_critical`（50）：互動式 scaffold 多問這三題。`check_threshold_unit_sanity` 的 TIER-INVERSION 改為：維度 warning 與 plain critical 比，維度 critical 不配對。
