---
title: "Rule Packs — 模組化 Prometheus 規則"
tags: [overview, introduction]
audience: [all]
version: v1.12.0
lang: zh
---
# Rule Packs — 模組化 Prometheus 規則

> 每個 Rule Pack 包含完整的三件套：Normalization Recording Rules + Threshold Normalization + Alert Rules。
> **所有 16 個 Rule Pack 已透過 Projected Volume 架構預載入 Prometheus 中** (分散於 `configmap-rules-*.yaml`)。
> 未部署 exporter 的 pack 不會產生 metrics，因此 alert 不會誤觸發 (near-zero cost)。
>
> **其他文件：** [Home](../index.md) (概覽) · [Migration Guide](../migration-guide.md) (遷移指南) · [Architecture & Design](../architecture-and-design.md) (技術深度)

## 支援的整合 (Supported Integrations)

| Rule Pack | File | Recording Rules | Alert Rules | Total |
|-----------|------|-----------------|-------------|-------|
| Clickhouse | rule-pack-clickhouse.yaml | 12 | 7 | 19 |
| DB2 | rule-pack-db2.yaml | 15 | 10 | 25 |
| Elasticsearch | rule-pack-elasticsearch.yaml | 14 | 11 | 25 |
| JVM | rule-pack-jvm.yaml | 9 | 7 | 16 |
| Kafka | rule-pack-kafka.yaml | 13 | 8 | 21 |
| Kubernetes | rule-pack-kubernetes.yaml | 30 | 14 | 44 |
| Liveness | rule-pack-liveness.yaml | 11 | 3 | 14 |
| Mariadb | rule-pack-mariadb.yaml | 12 | 17 | 29 |
| Mongodb | rule-pack-mongodb.yaml | 10 | 10 | 20 |
| Nginx | rule-pack-nginx.yaml | 9 | 6 | 15 |
| Operational | rule-pack-operational.yaml | 0 | 4 | 4 |
| Oracle | rule-pack-oracle.yaml | 13 | 9 | 22 |
| Platform | rule-pack-platform.yaml | 0 | 55 | 55 |
| Postgresql | rule-pack-postgresql.yaml | 8 | 8 | 16 |
| Rabbitmq | rule-pack-rabbitmq.yaml | 14 | 9 | 23 |
| Redis | rule-pack-redis.yaml | 12 | 9 | 21 |
| **TOTAL** | | **182** | **187** | **369** |

## 架構說明

每個 Rule Pack 擁有獨立的 ConfigMap (`k8s/03-monitoring/configmap-rules-*.yaml`)，
透過 Kubernetes **Projected Volume** 統一掛載至 Prometheus 的 `/etc/prometheus/rules/`。
各團隊 (DBA, K8s Infra, Search) 可獨立維護自己的 ConfigMap，不會產生 PR 衝突。
此目錄 (`rule-packs/`) 保留各 pack 的獨立 YAML 作為**權威參考 (canonical source)**，
方便查閱各 pack 的完整結構和 PromQL 表達式。

### 為什麼全部預載？

- **成本**: 沒有對應 metric 的 recording rule 會回傳空結果集，CPU 額外開銷 < 0.1%，evaluation 時間幾乎無增長。
- **簡化**: 新增 exporter 後只需配置 `_defaults.yaml` + tenant YAML，不需修改 Prometheus 設定。
- **安全**: 唯一的風險是 `absent()`：它在 metric 不存在時就觸發，所以只適合監看一定會被 scrape 的目標（例如 platform pack 監看 threshold-exporter 自己）。租戶 exporter 的存活不用 `absent()`，改由 `TenantExporterAbsent` 逐租戶判斷，只納管在 `_metadata` 宣告 `db_type` 的租戶。

### 動態卸載 (optional: true)

所有 Rule Pack 在 Projected Volume 中均設定 `optional: true`，這代表：

- **卸載不崩潰**: 刪除任何 Rule Pack 的 ConfigMap（`kubectl delete cm prometheus-rules-<type> -n monitoring`）後，Prometheus **不會 Crash**，只是對應的規則消失。
- **適用場景**: 大型客戶可能有自己的規則體系，需要關閉平台的黃金標準 Rule Pack，改用 `custom_` 前綴的遷移規則或完全自訂的規則。
- **重新載入**: 重新 `kubectl apply` 對應的 ConfigMap YAML 即可恢復。Prometheus 的 `--web.enable-lifecycle` 端點或 SHA-256 hash 偵測會自動觸發重載。

```bash
# 卸載 MongoDB Rule Pack（不影響其他 pack 和 Prometheus 運行）
kubectl delete cm prometheus-rules-mongodb -n monitoring

# 驗證 Prometheus 正常
kubectl logs -n monitoring deploy/prometheus --tail=5

# 恢復
kubectl apply -f k8s/03-monitoring/configmap-rules-mongodb.yaml
```

## 自訂 Rule Pack

每個 Rule Pack 遵循統一結構：

```yaml
groups:
  # 1. Normalization Recording Rules
  - name: <db>-normalization
    rules:
      - record: tenant:<metric>:<function>   # sum/max/rate5m
        expr: ...

  # 2. Threshold Normalization
  - name: <db>-threshold-normalization
    rules:
      - record: tenant:alert_threshold:<metric>
        expr: max by(tenant) (user_threshold{component="<component>", metric="<metric>", severity="warning"})

  # 3. Alert Rules (使用 group_left + unless maintenance；metadata 只在 annotations)
  - name: <db>-alerts
    rules:
      - alert: <AlertName>
        expr: |
          (
            tenant:<metric>:<function> > on(tenant) group_left tenant:alert_threshold:<metric>
          )
          unless on(tenant)
          (user_state_filter{filter="maintenance"} == 1)
        labels:
          severity: warning
          maintenance: mute   # 必填：mute｜page；上面兩行維護條件由 generate_rulepack_derived.py 依它寫入
        annotations:
          runbook_url: "{{ with query (printf `topk(1, tenant_metadata_info{tenant=%q})` $labels.tenant) }}{{ with index . 0 }}{{ .Labels.runbook_url }}{{ end }}{{ end }}"
          owner: "{{ with query (printf `topk(1, tenant_metadata_info{tenant=%q})` $labels.tenant) }}{{ with index . 0 }}{{ .Labels.owner }}{{ end }}{{ end }}"
          tier: "{{ with query (printf `topk(1, tenant_metadata_info{tenant=%q})` $labels.tenant) }}{{ with index . 0 }}{{ .Labels.tier }}{{ end }}{{ end }}"
```

### Dynamic Runbook Injection

租戶 metadata（runbook_url、owner、tier）來自 threshold-exporter 依租戶 `_metadata` 輸出的 `tenant_metadata_info`（值永遠為 1）。告警**不 join** 這個指標，而是在 annotations 裡用上面那個查詢樣板讀出來（`check_leftouterjoin_enrichment.py` 鎖定唯一寫法，並擋下任何 metadata join）。租戶沒有 `_metadata` 時樣板輸出空字串，告警照常觸發。

為什麼不 join（[#3483](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3483)）：join 會把 metadata 變成告警 label、也就是告警身分的一部分。threshold-exporter 跑多個副本、各自 reload（每隔 reloadInterval 檢查、ConfigMap 由各節點 kubelet 各自同步），改 metadata 後一兩分鐘內兩個副本會送出不同的值；join 每次評估可能挑到不同的那筆（VictoriaMetrics 的 `topk` 平手時就會），label 一變告警身分就變、`for:` 重來，該租戶在空窗內不觸發。放在 annotation 裡，空窗內頂多顯示其中一個副本的值，告警身分與觸發都不受影響。

內建 Rule Pack 帶 metadata 的告警都用這個樣板；**自訂 Rule Pack 也請照用**，不要用 `{{ $labels.runbook_url }}` 之類的 label 參照——告警上已沒有這些 label。依 runbook_url／owner／tier label 路由的 Alertmanager 設定要改成依 tenant 路由。

## Exporter 文件連結

- **mysqld_exporter**: https://github.com/prometheus/mysqld_exporter
- **redis_exporter**: https://github.com/oliver006/redis_exporter
- **mongodb_exporter**: https://github.com/percona/mongodb_exporter
- **elasticsearch_exporter**: https://github.com/prometheus-community/elasticsearch_exporter
- **oracledb_exporter**: https://github.com/iamseth/oracledb_exporter
- **ibm_db2_exporter**: https://github.com/IBM/db2-prometheus-exporter (community)
- **ClickHouse 內建 /metrics**：沒有獨立 exporter（本 pack 讀的指標名不是 clickhouse_exporter 的）
- **kafka_exporter**: https://github.com/danielqsj/kafka-exporter
- **rabbitmq_exporter**: https://github.com/kbudde/rabbitmq_exporter
- **kube-state-metrics**: https://github.com/kubernetes/kube-state-metrics
