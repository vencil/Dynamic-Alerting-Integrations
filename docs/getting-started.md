# Getting Started — threshold-exporter 使用指南

## 🎯 快速開始

### 前提條件

1. 已進入 Dev Container
2. Kind cluster (dynamic-alerting-cluster) 已建立
3. 基礎環境已部署 (`make setup`)

### 一鍵部署流程

```bash
cd ~/projects/dynamic-alerting-integrations

# 1. Build threshold-exporter image
make component-build COMP=threshold-exporter

# 2. Deploy to cluster
make component-deploy COMP=threshold-exporter ENV=local

# 3. Verify deployment
make component-test COMP=threshold-exporter

# 4. Run Scenario A test
./tests/scenario-a.sh db-a
```

---

## 📋 詳細步驟

### Step 1: Build & Load Image

```bash
# 這會執行：
# 1. cd ../threshold-exporter
# 2. docker build -t threshold-exporter:dev .
# 3. kind load docker-image threshold-exporter:dev --name dynamic-alerting-cluster

make component-build COMP=threshold-exporter
```

**預期輸出**：
```
Building threshold-exporter...
[+] Building 15.2s (12/12) FINISHED
✓ threshold-exporter:dev loaded into Kind cluster
```

**如果失敗**：
- 確認 `/sessions/friendly-compassionate-albattani/threshold-exporter` 存在
- 確認 Docker daemon 運行中
- 檢查 Kind cluster: `kind get clusters`

---

### Step 2: Deploy to Cluster

```bash
# 這會執行：
# kubectl apply -f components/threshold-exporter/
# 使用 environments/local/threshold-exporter.yaml 配置

make component-deploy COMP=threshold-exporter ENV=local
```

**預期輸出**：
```
Deploying threshold-exporter via kubectl...
deployment.apps/threshold-exporter created
service/threshold-exporter created
Waiting for threshold-exporter to be ready...
pod/threshold-exporter-xxx condition met
✓ threshold-exporter deployed (local environment)
```

**驗證部署**：
```bash
# 檢查 Pod 狀態
kubectl get pods -n monitoring -l app=threshold-exporter

# 檢查日誌
kubectl logs -n monitoring -l app=threshold-exporter --tail=20

# 檢查 Service
kubectl get svc -n monitoring threshold-exporter
```

---

### Step 3: 驗證功能

```bash
make component-test COMP=threshold-exporter
```

**這個測試會**：
1. ✓ 檢查 Pod 是否 Running
2. ✓ 測試 `/health` endpoint
3. ✓ 測試 `/metrics` endpoint
4. ✓ 驗證預設閾值已載入
5. ✓ 測試 POST API 設定新閾值
6. ✓ 驗證新閾值出現在 metrics
7. ✓ 檢查 Prometheus 是否成功 scrape

**預期輸出**：
```
[✓] Verifying threshold-exporter...
[✓] Pod is running
[✓] Service exists
[✓] Health check passed
[✓] Metrics endpoint working
[✓] Default thresholds loaded
[✓] Threshold API working
[✓] New threshold value appears in metrics
====================================================
✓ threshold-exporter verification completed
====================================================
```

---

### Step 4: 手動測試 API

#### 4.1 Port Forward

```bash
kubectl port-forward -n monitoring svc/threshold-exporter 8080:8080 &
```

#### 4.2 查看預設閾值

```bash
curl http://localhost:8080/api/v1/thresholds | jq
```

**輸出**：
```json
[
  {
    "tenant": "db-a",
    "component": "mysql",
    "metric": "cpu",
    "value": 80,
    "severity": "warning"
  },
  {
    "tenant": "db-a",
    "component": "mysql",
    "metric": "connections",
    "value": 80,
    "severity": "warning"
  }
]
```

#### 4.3 設定新閾值

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

**輸出**：
```json
{
  "status": "success",
  "message": "Threshold set successfully"
}
```

#### 4.4 檢查 Prometheus Metrics

```bash
curl http://localhost:8080/metrics | grep user_threshold
```

**輸出**：
```
user_threshold{component="mysql",metric="connections",severity="warning",tenant="db-a"} 75
user_threshold{component="mysql",metric="connections",severity="warning",tenant="db-b"} 80
user_threshold{component="mysql",metric="cpu",severity="warning",tenant="db-a"} 80
user_threshold{component="mysql",metric="cpu",severity="warning",tenant="db-b"} 80
```

---

### Step 5: 驗證 Prometheus 整合

#### 5.1 確認 Prometheus 已 Scrape

```bash
# Port forward Prometheus
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &

# 查詢閾值 metrics
curl -s "http://localhost:9090/api/v1/query?query=user_threshold" | jq
```

#### 5.2 在 Prometheus UI 查看

1. 打開瀏覽器: http://localhost:9090
2. 到 Graph 頁面
3. 輸入查詢: `user_threshold{tenant="db-a"}`
4. 點擊 Execute

**應該看到**：
```
user_threshold{component="mysql", metric="connections", severity="warning", tenant="db-a"} = 75
user_threshold{component="mysql", metric="cpu", severity="warning", tenant="db-a"} = 80
```

#### 5.3 檢查 Recording Rules

```promql
# 查看 recording rule 結果
tenant:alert_threshold:connections{tenant="db-a"}
```

這個 recording rule 會：
- 優先使用 `user_threshold{component="mysql", metric="connections"}`
- 如果沒有，fallback 到預設值 80

---

## 🧪 執行 Scenario A 測試

這是完整的端到端測試，驗證動態閾值功能。

```bash
./tests/scenario-a.sh db-a
```

### 測試流程

1. **設定初始閾值（70）**
   - POST /api/v1/threshold
   - 等待 Prometheus scrape

2. **檢查當前連線數**
   - 查詢 `mysql_global_status_threads_connected`

3. **製造負載（如果需要）**
   - 啟動多個 MySQL 連線
   - 確保超過閾值

4. **驗證 Alert Firing**
   - 檢查 recording rule: `tenant:alert_threshold:connections`
   - 檢查 alert 狀態

5. **調高閾值（80）**
   - POST /api/v1/threshold (value=80)
   - 等待 Prometheus scrape

6. **驗證 Alert Resolved**
   - 連線數現在低於新閾值
   - Alert 應該自動解除

### 預期輸出

```
==========================================
Scenario A: Dynamic Thresholds Test
==========================================

Phase 1: Environment Setup
[✓] All required services are running

Phase 2: Set initial threshold (connections = 70)
[✓] Initial threshold set: connections = 70

Phase 3: Waiting for Prometheus to scrape threshold...
[✓] Prometheus scraped threshold: 70

Phase 4: Check current connection count
Current connections for db-a: 5

Phase 5: Generate load if needed
[!] Current connections (5) < threshold (70)
[!] Simulating high connection load...
Waiting for connections to increase...
New connection count: 8

Phase 6: Verify alert should be FIRING
Checking recording rule: tenant:alert_threshold:connections
Threshold from recording rule: 70
[✓] Conditions met for alert: 8 > 70
[✓] Alert is FIRING (as expected)

Phase 7: Increase threshold to 80
[✓] Threshold updated: connections = 80

Phase 8: Waiting for new threshold to take effect...
[✓] New threshold scraped: 80

Phase 9: Verify alert should be RESOLVED
Current connections: 8, New threshold: 80
[✓] Connections (8) now below threshold (80)
Waiting for alert to resolve...
[✓] Alert is RESOLVED (as expected)

==========================================
Scenario A Test Summary
==========================================

Test Steps Completed:
  ✓ 1. Set threshold to 70
  ✓ 2. Prometheus scraped threshold
  ✓ 3. Checked current connections
  ✓ 4. Generated load if needed
  ✓ 5. Verified alert conditions
  ✓ 6. Increased threshold to 80
  ✓ 7. Prometheus scraped new threshold
  ✓ 8. Verified alert resolution conditions

Key Metrics:
  - Initial threshold: 70
  - Current connections: 8
  - New threshold: 80
  - Alert status: inactive

✓ Scenario A: Dynamic Thresholds Test Completed
```

---

## 🔍 疑難排解

### 問題 1: Pod 無法啟動

```bash
# 檢查 Pod 狀態
kubectl get pods -n monitoring -l app=threshold-exporter

# 查看詳細錯誤
kubectl describe pod -n monitoring -l app=threshold-exporter

# 查看日誌
kubectl logs -n monitoring -l app=threshold-exporter
```

**常見原因**：
- Image 沒有正確 load 到 Kind: `make component-build`
- ImagePullPolicy 設定錯誤: 確認是 `Never`
- 資源不足: 檢查 Kind cluster 記憶體

### 問題 2: Prometheus 沒有 Scrape

```bash
# 檢查 Prometheus 配置
kubectl get cm -n monitoring prometheus-config -o yaml | grep threshold-exporter

# 檢查 Prometheus targets
# 在 http://localhost:9090/targets 查看
# 應該看到 threshold-exporter (1/1 up)
```

**解決方案**：
1. 確認 Prometheus ConfigMap 包含 threshold-exporter scrape config
2. 重啟 Prometheus: `kubectl rollout restart deployment/prometheus -n monitoring`
3. 檢查 Service annotations:
   ```bash
   kubectl get svc threshold-exporter -n monitoring -o yaml | grep annotations -A 3
   ```

### 問題 3: API 回傳 404

```bash
# 確認 port-forward 正確
kubectl port-forward -n monitoring svc/threshold-exporter 8080:8080

# 測試 health endpoint
curl http://localhost:8080/health

# 檢查 Service 的 endpoint
kubectl get endpoints -n monitoring threshold-exporter
```

### 問題 4: Recording Rule 沒有資料

```promql
# 檢查原始 metric 是否存在
user_threshold{tenant="db-a"}

# 檢查 recording rule 配置
kubectl get cm -n monitoring prometheus-config -o yaml | grep -A 20 "recording-rules.yml"

# 重新載入 Prometheus 配置
kubectl exec -n monitoring deploy/prometheus -- killall -HUP prometheus
```

---

## 📊 監控與觀察

### Grafana Dashboard（待建立）

可以建立 Grafana dashboard 顯示：
1. 所有 tenants 的動態閾值
2. 當前 metrics vs 閾值的比較
3. 閾值變更歷史
4. Alert 觸發歷史

### Prometheus Queries

```promql
# 1. 查看所有閾值
user_threshold

# 2. 比較實際值與閾值
mysql_global_status_threads_connected
  and on(tenant)
  user_threshold{component="mysql", metric="connections"}

# 3. 計算距離閾值的差距
(
  mysql_global_status_threads_connected
  -
  on(tenant) group_left
  user_threshold{component="mysql", metric="connections"}
) / on(tenant) group_left user_threshold{component="mysql", metric="connections"} * 100

# 4. 查看哪些 tenants 超過閾值
mysql_global_status_threads_connected
  > on(tenant) group_left
  tenant:alert_threshold:connections
```

---

## 🚀 下一步

### Scenario B: Weakest Link Detection

實作 container-level monitoring，檢測 Pod 內最弱的 container。

### Scenario C: State Matching

使用 kube-state-metrics 監控 Pod phase 和 container status。

### Scenario D: Composite Priority Logic

實作複雜的條件邏輯和 fallback rules。

---

## 📚 參考資料

- [Architecture Review](./architecture-review.md)
- [Deployment Guide](./deployment-guide.md)
- [Week 1 Summary](./week1-summary.md)
- [threshold-exporter README](../../threshold-exporter/README.md)
