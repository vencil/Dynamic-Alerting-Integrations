# 🚀 Quick Start Guide

## 在 Dev Container 中執行完整測試

### 方式 1: 一鍵執行

```bash
# 1. 在 VS Code 中打開專案
code /path/to/dynamic-alerting-integrations

# 2. 按 F1 → "Dev Containers: Reopen in Container"

# 3. 等待容器啟動完成

# 4. 執行測試腳本
./RUN-TESTS.sh
```

這個腳本會自動執行：
- ✅ 部署基礎環境 (MariaDB + Monitoring)
- ✅ 部署 kube-state-metrics
- ✅ Build threshold-exporter image
- ✅ 部署 threshold-exporter
- ✅ 執行驗證測試
- ✅ 執行 Scenario A 測試

---

### 方式 2: 手動逐步執行

#### Step 1: 進入 Dev Container

```bash
# VS Code → F1 → "Dev Containers: Reopen in Container"
```

#### Step 2: 部署基礎環境

```bash
make setup
make status
```

#### Step 3: 部署 kube-state-metrics

```bash
./scripts/deploy-kube-state-metrics.sh
```

#### Step 4: Build & Deploy threshold-exporter

```bash
# Build image
make component-build COMP=threshold-exporter

# Deploy to cluster
make component-deploy COMP=threshold-exporter ENV=local
```

#### Step 5: 驗證部署

```bash
# 檢查 Pod 狀態
kubectl get pods -n monitoring -l app=threshold-exporter

# 執行驗證測試
make component-test COMP=threshold-exporter
```

#### Step 6: 執行 Scenario A 測試

```bash
./tests/scenario-a.sh db-a
```

---

## 🔍 快速測試 API

### 設定 Port Forward

```bash
# Terminal 1: Prometheus
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &

# Terminal 2: threshold-exporter
kubectl port-forward -n monitoring svc/threshold-exporter 8080:8080 &

# Terminal 3: Grafana
kubectl port-forward -n monitoring svc/grafana 3000:3000 &
```

或使用 Makefile：

```bash
make port-forward
```

### 測試 threshold-exporter API

#### 1. 查看預設閾值

```bash
curl http://localhost:8080/api/v1/thresholds | jq
```

#### 2. 設定新閾值

```bash
curl -X POST http://localhost:8080/api/v1/threshold \
  -H "Content-Type: application/json" \
  -d '{
    "tenant": "db-a",
    "component": "mysql",
    "metric": "connections",
    "value": 75,
    "severity": "warning"
  }'
```

#### 3. 檢查 Prometheus Metrics

```bash
curl http://localhost:8080/metrics | grep user_threshold
```

#### 4. 在 Prometheus 查詢

```bash
# 方法 1: API
curl -s "http://localhost:9090/api/v1/query?query=user_threshold" | jq

# 方法 2: Web UI
# 打開瀏覽器: http://localhost:9090
# 輸入查詢: user_threshold{tenant="db-a"}
```

---

## 📊 驗證動態閾值功能

### 完整流程測試

```bash
# 1. 設定低閾值
curl -X POST http://localhost:8080/api/v1/threshold \
  -H "Content-Type: application/json" \
  -d '{"tenant":"db-a","component":"mysql","metric":"connections","value":5}'

# 2. 等待 Prometheus scrape (15-30s)
sleep 30

# 3. 查詢閾值
curl -s "http://localhost:9090/api/v1/query?query=user_threshold{tenant=\"db-a\",metric=\"connections\"}" | jq '.data.result[0].value'

# 4. 查詢當前連線數
curl -s "http://localhost:9090/api/v1/query?query=mysql_global_status_threads_connected{tenant=\"db-a\"}" | jq '.data.result[0].value'

# 5. 檢查 Alert 狀態
curl -s "http://localhost:9090/api/v1/alerts" | jq '.data.alerts[] | select(.labels.alertname=="MariaDBHighConnections")'

# 6. 調高閾值
curl -X POST http://localhost:8080/api/v1/threshold \
  -H "Content-Type: application/json" \
  -d '{"tenant":"db-a","component":"mysql","metric":"connections","value":90}'

# 7. 再次檢查 Alert (等待 1-2 分鐘)
sleep 90
curl -s "http://localhost:9090/api/v1/alerts" | jq '.data.alerts[] | select(.labels.alertname=="MariaDBHighConnections")'
```

---

## 🛠️ 常用指令

### 檢查狀態

```bash
# 所有 Pods
make status

# threshold-exporter logs
make component-logs COMP=threshold-exporter

# inspect tenant 健康度
make inspect-tenant TENANT=db-a
```

### 重新部署

```bash
# 重建 image
make component-build COMP=threshold-exporter

# 重新部署
kubectl delete deployment threshold-exporter -n monitoring
make component-deploy COMP=threshold-exporter

# 或一鍵重建
make component-build COMP=threshold-exporter && \
kubectl rollout restart deployment/threshold-exporter -n monitoring
```

### 清理環境

```bash
# 清除所有資源（保留 cluster）
make clean

# 完全重置
make destroy
kind create cluster --name dynamic-alerting-cluster
make setup
```

---

## 🎯 測試清單

完成以下檢查確認系統正常：

- [ ] 基礎環境部署成功 (`make status` 所有 Pods Running)
- [ ] kube-state-metrics 運行中
- [ ] threshold-exporter Pod 狀態為 Running
- [ ] Health check 通過 (`curl http://localhost:8080/health`)
- [ ] Metrics endpoint 有資料 (`curl http://localhost:8080/metrics | grep user_threshold`)
- [ ] Prometheus 成功 scrape threshold metrics
- [ ] 可以透過 API 設定新閾值
- [ ] 新閾值出現在 Prometheus 中
- [ ] Recording rule 運作正常 (`tenant:alert_threshold:connections`)
- [ ] Scenario A 測試通過

---

## 📚 參考文件

- [Getting Started (詳細版)](docs/getting-started.md)
- [Deployment Guide](docs/deployment-guide.md)
- [Architecture Review](docs/architecture-review.md)
- [threshold-exporter README](../threshold-exporter/README.md)

---

## 🆘 遇到問題？

### 問題 1: Kind cluster 不存在

```bash
kind create cluster --name dynamic-alerting-cluster
make setup
```

### 問題 2: Image 沒有 load 到 Kind

```bash
make component-build COMP=threshold-exporter
kind load docker-image threshold-exporter:dev --name dynamic-alerting-cluster
```

### 問題 3: Pod 一直 Pending 或 CrashLoopBackOff

```bash
kubectl describe pod -n monitoring -l app=threshold-exporter
kubectl logs -n monitoring -l app=threshold-exporter --tail=50
```

### 問題 4: Prometheus 沒有 scrape 到 metrics

```bash
# 檢查 Prometheus config
kubectl get cm -n monitoring prometheus-config -o yaml | grep threshold-exporter

# 重啟 Prometheus
kubectl rollout restart deployment/prometheus -n monitoring

# 檢查 targets
# http://localhost:9090/targets → 應該看到 threshold-exporter (1/1 up)
```

---

## ✅ 成功標誌

如果看到以下輸出，表示一切正常：

```bash
$ ./RUN-TESTS.sh

==========================================
Dynamic Alerting Integrations
Complete Test Workflow
==========================================

[i] Phase 0: Pre-flight checks
[✓] ✓ Environment ready

[i] Phase 1: Deploy base infrastructure
[✓] ✓ Base infrastructure running

[i] Phase 2: Deploy kube-state-metrics
[✓] ✓ kube-state-metrics running

[i] Phase 3: Build threshold-exporter image
[✓] ✓ threshold-exporter:dev image loaded to Kind

[i] Phase 4: Deploy threshold-exporter
[✓] ✓ threshold-exporter deployed

[i] Phase 5: Verification test
[✓] ✓ Component verification passed

[i] Phase 6: Scenario A - Dynamic Thresholds Test
[✓] ✓ Scenario A test completed

[i] Phase 7: System status check
...

[i] ==========================================
[i] All Tests Completed Successfully!
[i] ==========================================
```

**恭喜！Dynamic Alerting Integrations 已經成功部署並測試！** 🎉
