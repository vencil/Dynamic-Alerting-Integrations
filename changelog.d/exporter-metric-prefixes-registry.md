---
section: Changed
topic: tools
issues: [4270]
created: 2026-11-24T23:30:00+08:00
---
- **exporter 的指標命名空間只寫在 threshold registry（tools；[#4270](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4270)）**：`rule-packs/threshold-registry.yaml` 新增 `packs.<pack>.metric_prefixes`，帶 `presence:` 的 pack 必寫。scrape gate 與 rule pack 拆分驗證器判定「租戶 exporter 的指標」、`onboard_platform` 推 `db_types`、`analyze_rule_pack_gaps` 的前綴比對與 `discover_instance_mappings` 的 DB 種類，都改由它推導，刪除四份互相不一致的手寫前綴表。ClickHouse 改用實際名稱 `ClickHouse`（伺服器內建 `/metrics` 以 `ClickHouseMetrics_` 等命名）。⚠️ Breaking：`clickhouse_*`（例如舊的第三方 clickhouse_exporter）不再判成租戶 exporter 的指標，自備規則跑 `generate_rule_pack_split` 時，central 規則讀它會報缺少來源；`mariadb_`、`postgres_`、`es_`、`mongo_`、`rabbit_`、`oracle_`、`mssql_`、`mysqld_`、`kafka_topic_`，以及 kubernetes 的 `node_`、`pod_` 等不在 registry 的前綴不再對應 pack（onboarding 的啟發式比對不分大小寫，舊 exporter 的 `clickhouse_*` 在那裡仍提示 ClickHouse）；`discover-mappings` 產生的草稿註解裡 `db_type` 改寫 pack 名稱（`postgresql`，不再是 `pg`）。由新測試雙向釘住：pack 規則讀到的原始指標都在自家前綴內、每個前綴都有自家規則或 presence 讀到、前綴之間與平台家族互不重疊。
