# Threshold Exporter

**核心 Component** - 將使用者設定的動態閾值轉換為 Prometheus metrics，實現 Scenario A (Dynamic Thresholds)。

## 功能

- HTTP API 接收閾值設定
- 轉換為 Prometheus `/metrics` endpoint
- 支援多 tenant 配置

## API

```bash
# 設定閾值
POST /api/v1/threshold
Content-Type: application/json

{
  "tenant": "db-a",
  "component": "mysql",
  "metric": "cpu",
  "value": 70,
  "severity": "warning"
}

# 查看所有閾值
GET /api/v1/thresholds

# Prometheus metrics endpoint
GET /metrics
```

## Metrics 輸出格式

統一使用 `user_threshold` gauge，透過 label 區分 metric 類型：

```prometheus
# HELP user_threshold User-defined dynamic threshold
# TYPE user_threshold gauge
user_threshold{tenant="db-a",component="mysql",metric="cpu",severity="warning"} 70
user_threshold{tenant="db-a",component="mysql",metric="connections",severity="warning"} 80
user_threshold{tenant="db-b",component="mysql",metric="cpu",severity="warning"} 80
```

## Prometheus 整合

Recording rules 會消費此 metric 並產生 normalized thresholds：

```yaml
# recording rule (已在 configmap-prometheus.yaml 中定義)
- record: tenant:alert_threshold:cpu
  expr: sum by(tenant) (user_threshold{metric="cpu"}) or (max by(tenant) (mysql_up) * 80)

- record: tenant:alert_threshold:connections
  expr: sum by(tenant) (user_threshold{metric="connections"}) or (max by(tenant) (mysql_up) * 80)
```

Service Discovery 會透過 `prometheus.io/scrape: "true"` annotation 自動發現此 exporter。

## 開發狀態

待實作 - 請參考獨立 repo: `threshold-exporter`

## 本地測試

```bash
# Build & Deploy (via Helm chart)
make component-build COMP=threshold-exporter
make component-deploy COMP=threshold-exporter ENV=local

# 測試 API
curl -X POST http://localhost:8080/api/v1/threshold \
  -H "Content-Type: application/json" \
  -d '{"tenant":"db-a","component":"mysql","metric":"cpu","value":70}'

# 驗證 metrics
curl http://localhost:8080/metrics | grep user_threshold
```
