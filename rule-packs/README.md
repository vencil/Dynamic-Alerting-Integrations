# Rule Packs — 模組化 Prometheus 規則

> 每個 Rule Pack 包含完整的三件套：Normalization Recording Rules + Threshold Normalization + Alert Rules。
> 掛載到 Prometheus 即可使用，無需自行撰寫 PromQL。

## 支援的整合 (Supported Integrations)

| Rule Pack | Exporter | 狀態 | Recording Rules | Alert Rules |
|-----------|----------|------|----------------|------------|
| **kubernetes** | cAdvisor + kube-state-metrics | 🟢 預設啟用 | 5 | 4 |
| **mariadb** | mysqld_exporter (Percona) | 🟢 預設啟用 | 7 | 8 |
| **redis** | oliver006/redis_exporter | 🟡 選配 | 7 | 6 |
| **mongodb** | percona/mongodb_exporter | 🟡 選配 | 7 | 6 |
| **elasticsearch** | elasticsearch_exporter | 🟡 選配 | 7 | 7 |

## 快速啟用

### 方法 1: 直接掛載 (kubectl)

```bash
# 1. 將 rule pack 加入 Prometheus ConfigMap
kubectl create configmap prometheus-rules-redis \
  --from-file=rule-pack-redis.yml=rule-packs/rule-pack-redis.yaml \
  -n monitoring

# 2. 掛載到 Prometheus Pod (修改 deployment)
# Volume: configMap → prometheus-rules-redis
# Mount:  /etc/prometheus/rules/rule-pack-redis.yml
```

### 方法 2: Helm values overlay (推薦)

```bash
# 安裝時啟用 Redis + MongoDB rule packs
helm upgrade --install threshold-exporter ./components/threshold-exporter \
  -n monitoring \
  -f environments/local/threshold-exporter.yaml \
  -f rule-packs/rule-pack-redis.yaml \
  -f rule-packs/rule-pack-mongodb.yaml
```

### 方法 3: 合併到現有 ConfigMap

```bash
# 將 rule pack 的 groups 追加到 configmap-prometheus.yaml 的 recording-rules.yml / alert-rules.yml 中
# 參考 configmap-prometheus.yaml 的格式
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
        expr: sum by(tenant) (user_threshold{metric="<metric>", severity="warning"})

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
