---
title: "Rule Packs — 模組化 Prometheus 規則"
tags: [overview, introduction]
audience: [all]
version: v1.12.0
lang: zh
---
# Rule Packs — 模組化 Prometheus 規則

> 每個 Rule Pack 包含完整的三件套：Normalization Recording Rules + Threshold Normalization + Alert Rules。
> **所有 15 個 Rule Pack 已透過 Projected Volume 架構預載入 Prometheus 中** (分散於 `configmap-rules-*.yaml`)。
> 未部署 exporter 的 pack 不會產生 metrics，因此 alert 不會誤觸發 (near-zero cost)。
>
> **其他文件：** [Home](../index.md) (概覽) · [Migration Guide](../migration-guide.md) (遷移指南) · [Architecture & Design](../architecture-and-design.md) (技術深度)

## 支援的整合 (Supported Integrations)

| Rule Pack | File | Recording Rules | Alert Rules | Total |
|-----------|------|-----------------|-------------|-------|
| Clickhouse | rule-pack-clickhouse.yaml | 12 | 7 | 19 |
| DB2 | rule-pack-db2.yaml | 12 | 7 | 19 |
| Elasticsearch | rule-pack-elasticsearch.yaml | 11 | 7 | 18 |
| JVM | rule-pack-jvm.yaml | 9 | 7 | 16 |
| Kafka | rule-pack-kafka.yaml | 13 | 9 | 22 |
| Kubernetes | rule-pack-kubernetes.yaml | 7 | 4 | 11 |
| Mariadb | rule-pack-mariadb.yaml | 11 | 8 | 19 |
| Mongodb | rule-pack-mongodb.yaml | 10 | 6 | 16 |
| Nginx | rule-pack-nginx.yaml | 9 | 6 | 15 |
| Operational | rule-pack-operational.yaml | 0 | 4 | 4 |
| Oracle | rule-pack-oracle.yaml | 11 | 7 | 18 |
| Postgresql | rule-pack-postgresql.yaml | 11 | 9 | 20 |
| Rabbitmq | rule-pack-rabbitmq.yaml | 12 | 8 | 20 |
| Redis | rule-pack-redis.yaml | 11 | 6 | 17 |
| **TOTAL** | | **139** | **95** | **234** |

## 架構說明

每個 Rule Pack 擁有獨立的 ConfigMap (`k8s/03-monitoring/configmap-rules-*.yaml`)，
透過 Kubernetes **Projected Volume** 統一掛載至 Prometheus 的 `/etc/prometheus/rules/`。
各團隊 (DBA, K8s Infra, Search) 可獨立維護自己的 ConfigMap，不會產生 PR 衝突。
此目錄 (`rule-packs/`) 保留各 pack 的獨立 YAML 作為**權威參考 (canonical source)**，
方便查閱各 pack 的完整結構和 PromQL 表達式。

### 為什麼全部預載？

- **成本**: 沒有對應 metric 的 recording rule 會回傳空結果集，CPU 額外開銷 < 0.1%，evaluation 時間幾乎無增長。
- **簡化**: 新增 exporter 後只需配置 `_defaults.yaml` + tenant YAML，不需修改 Prometheus 設定。
- **安全**: 唯一的風險是 `absent()` — 目前只有 mariadb (已部署) 使用 `absent(mysql_up)`，其他 pack 都不含 `absent()`。

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
        expr: max by(tenant) (user_threshold{metric="<metric>", severity="warning"})

  # 3. Alert Rules (使用 group_left + unless maintenance + runbook injection)
  - name: <db>-alerts
    rules:
      - alert: <AlertName>
        expr: |
          (
            tenant:<metric>:<function> > on(tenant) group_left tenant:alert_threshold:<metric>
          )
          * on(tenant) group_left(runbook_url, owner, tier) tenant_metadata_info
          unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
        annotations:
          runbook_url: "{{ $labels.runbook_url }}"
          owner: "{{ $labels.owner }}"
          tier: "{{ $labels.tier }}"
```

### Dynamic Runbook Injection (v1.11.0)

Alert Rules 透過 `* on(tenant) group_left(runbook_url, owner, tier) tenant_metadata_info` 將租戶 metadata 注入 alert labels，再由 annotations 引用。`tenant_metadata_info` 由 threshold-exporter 根據租戶 `_metadata` 配置自動輸出（值永遠為 1），保證 `group_left` join 不會漏掉任何 tenant。

若租戶未設定 `_metadata`，`tenant_metadata_info` 不存在，`group_left` 回傳空向量。因此已內建的 11 個 Rule Pack 均已加入此 join，但 **自訂 Rule Pack 建議同步採用此 pattern** 以確保 runbook URL 與 owner 資訊可自動傳遞至通知。

## Exporter 文件連結

- **mysqld_exporter**: https://github.com/prometheus/mysqld_exporter
- **redis_exporter**: https://github.com/oliver006/redis_exporter
- **mongodb_exporter**: https://github.com/percona/mongodb_exporter
- **elasticsearch_exporter**: https://github.com/prometheus-community/elasticsearch_exporter
- **oracledb_exporter**: https://github.com/iamseth/oracledb_exporter
- **ibm_db2_exporter**: https://github.com/IBM/db2-prometheus-exporter (community)
- **clickhouse_exporter**: https://github.com/ClickHouse/clickhouse_exporter (或 ClickHouse 內建 /metrics)
- **kafka_exporter**: https://github.com/danielqsj/kafka-exporter
- **rabbitmq_exporter**: https://github.com/kbudde/rabbitmq_exporter
- **kube-state-metrics**: https://github.com/kubernetes/kube-state-metr