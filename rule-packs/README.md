# Rule Packs — 模組化 Prometheus 規則

> 每個 Rule Pack 包含完整的三件套：Normalization Recording Rules + Threshold Normalization + Alert Rules。
> **所有 6 個 Rule Pack 已透過 Projected Volume 架構預載入 Prometheus 中** (分散於 `configmap-rules-*.yaml`)。
> 未部署 exporter 的 pack 不會產生 metrics，因此 alert 不會誤觸發 (near-zero cost)。

## 支援的整合 (Supported Integrations)

| Rule Pack | Exporter | 狀態 | Recording Rules | Alert Rules |
|-----------|----------|------|----------------|------------|
| **kubernetes** | cAdvisor + kube-state-metrics | 🟢 預載 | 5 | 4 |
| **mariadb** | mysqld_exporter (Percona) | 🟢 預載 | 7 | 8 |
| **redis** | oliver006/redis_exporter | 🟢 預載 | 7 | 6 |
| **mongodb** | percona/mongodb_exporter | 🟢 預載 | 7 | 6 |
| **elasticsearch** | elasticsearch_exporter | 🟢 預載 | 7 | 7 |
| **platform** | threshold-exporter self-monitoring | 🟢 預載 | 0 | 4 |

## 架構說明

每個 Rule Pack 擁有獨立的 ConfigMap (`k8s/03-monitoring/configmap-rules-*.yaml`)，
透過 Kubernetes **Projected Volume** 統一掛載至 Prometheus 的 `/etc/prometheus/rules/`。
各團隊 (DBA, K8s Infra, Search) 可獨立維護自己的 ConfigMap，不會產生 PR 衝突。
此目錄 (`rule-packs/`) 保留各 pack 的獨立 YAML 作為**權威參考 (canonical source)**，
方便查閱各 pack 的完整結構和 PromQL 表達式。

### 為什麼全部預載？

- **成本**: 沒有對應 metric 的 recording rule 會回傳空結果集，不佔 CPU/memory。
- **簡化**: 新增 exporter 後只需配置 `_defaults.yaml` + tenant YAML，不需修改 Prometheus 設定。
- **安全**: 唯一的風險是 `absent()` — 目前只有 mariadb (已部署) 使用 `absent(mysql_up)`，其他 pack 都不含 `absent()`。

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

  # 3. Alert Rules (使用 group_left + unless maintenance)
  - name: <db>-alerts
    rules:
      - alert: <AlertName>
        expr: |
          ( tenant:<metric>:<function> > on(tenant) group_left tenant:alert_threshold:<metric> )
          unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
```

## Exporter 文件連結

- **mysqld_exporter**: https://github.com/prometheus/mysqld_exporter
- **redis_exporter**: https://github.com/oliver006/redis_exporter
- **mongodb_exporter**: https://github.com/percona/mongodb_exporter
- **elasticsearch_exporter**: https://github.com/prometheus-community/elasticsearch_exporter
- **kube-state-metrics**: https://github.com/kubernetes/kube-state-metrics
