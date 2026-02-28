# Bring Your Own Prometheus (BYOP) — 現有監控架構整合指南

> **受眾**：Platform Engineers、SREs
> **前置閱讀**：[架構與設計](architecture-and-design.md) §1–§3（向量匹配與 Projected Volume 原理）
> **版本**：v0.10.0

---

## 概述

本平台採用**非侵入式 (Non-invasive)** 設計。如果你的組織已經擁有自建的 Prometheus、Thanos 或 VictoriaMetrics 叢集，**你不需要替換它**。

只要完成以下 **3 個最小整合步驟**，你的現有監控基礎設施就能無縫啟用動態閾值警報引擎：

| 步驟 | 動作 | 耗時估計 |
|------|------|----------|
| 1 | 注入 `tenant` 標籤 | ~5 分鐘 |
| 2 | 抓取 `threshold-exporter` | ~2 分鐘 |
| 3 | 掛載黃金規則包 (Rule Packs) | ~5 分鐘 |

整合後，你的 Prometheus 會新增：1 個 relabel 設定、1 個 scrape job、以及 6 個 Rule Pack ConfigMap（可選擇性掛載）。**現有的 scrape job、recording rule、alerting rule 完全不受影響。**

---

## 前提：為什麼需要 `tenant` 標籤？

本平台的核心機制是透過 `group_left` 向量匹配，將**租戶的即時指標**與 `threshold-exporter` 吐出的**動態閾值**進行比對：

```promql
# 簡化範例：當實際連線數超過該租戶的自訂閾值時觸發
mysql_global_status_threads_connected
  > on(tenant) group_left()
user_threshold_connections
```

這要求兩邊的指標**都必須帶有相同的 `tenant` 標籤**。`threshold-exporter` 的指標天生自帶 `tenant`，但你的資料庫 exporter（如 mysqld_exporter、redis_exporter）吐出的指標**預設沒有**。如果 `tenant` 標籤不匹配，`group_left` 會靜默返回空向量，所有警報都不會觸發。

---

## 步驟 1：注入 `tenant` 標籤

### 目標

讓你現有 Prometheus 抓取的資料庫指標都帶上 `tenant` 標籤，使其能與 `threshold-exporter` 的閾值向量配對。

### 方法：利用 K8s Service Discovery 的 relabel_configs

在你現有的 `scrape_configs` 中（抓取資料庫 exporter 的那些 job），加入以下 `relabel_configs`：

**方案 A — 以 Namespace 作為 tenant 名稱**（推薦，適用於一個 tenant 一個 namespace 的架構）

```yaml
scrape_configs:
  - job_name: "tenant-db-exporters"
    scrape_interval: 10s
    kubernetes_sd_configs:
      - role: service
    relabel_configs:
      # 只保留帶有 scrape 標記的 Service
      - source_labels: [__meta_kubernetes_service_annotation_prometheus_io_scrape]
        action: keep
        regex: "true"
      # 只保留 tenant namespace（依你的命名規則調整 regex）
      - source_labels: [__meta_kubernetes_namespace]
        action: keep
        regex: "db-.+"                    # ← 調整為你的 tenant namespace 命名模式
      # ★ 核心：將 namespace 名稱注入為 tenant 標籤
      - source_labels: [__meta_kubernetes_namespace]
        target_label: tenant
      # 使用 Service annotation 指定的 port
      - source_labels: [__address__, __meta_kubernetes_service_annotation_prometheus_io_port]
        action: replace
        target_label: __address__
        regex: ([^:]+)(?::\d+)?;(\d+)
        replacement: $1:$2
```

**方案 B — 以自訂 Label 作為 tenant 名稱**（適用於多個 tenant 共用 namespace 的架構）

```yaml
relabel_configs:
  # 從 Service 的 K8s label 讀取 tenant 名稱
  - source_labels: [__meta_kubernetes_service_label_tenant]
    target_label: tenant
```

> **⚠️ 重要**：`tenant` 標籤的值必須與 `threshold-exporter` ConfigMap 中的 tenant 名稱完全一致（例如 `db-a`、`db-b`）。請用 `scaffold_tenant.py` 產生的名稱作為基準。

### 驗證

注入 `tenant` 標籤後，等待一個 scrape interval（預設 10–15 秒），然後驗證：

```bash
# 1. 確認指標帶有 tenant 標籤
curl -s 'http://<your-prometheus>:9090/api/v1/query?query=mysql_global_status_threads_connected' \
  | jq '.data.result[].metric.tenant'

# 預期輸出（每個 tenant 一筆）：
# "db-a"
# "db-b"

# 2. 如果沒有輸出或值為 null，檢查 target 是否被正確發現
curl -s 'http://<your-prometheus>:9090/api/v1/targets' \
  | jq '.data.activeTargets[] | select(.labels.job=="tenant-db-exporters") | {instance: .labels.instance, tenant: .labels.tenant, health: .health}'
```

**✅ 通過條件**：每個 tenant 的指標都帶有正確的 `tenant` 標籤值。

---

## 步驟 2：抓取 threshold-exporter

### 目標

讓你的 Prometheus 知道去哪裡讀取動態閾值指標（`user_threshold_*` 系列）。

### 設定

在你的 `prometheus.yml` 中新增一個 scrape job：

```yaml
scrape_configs:
  # ... 你現有的 jobs ...

  # ★ 動態閾值引擎
  - job_name: "dynamic-thresholds"
    scrape_interval: 15s
    # 方式一：靜態配置（最簡單）
    static_configs:
      - targets: ["threshold-exporter.monitoring.svc.cluster.local:8080"]
    # 方式二：K8s Service Discovery（自動發現，推薦生產環境）
    # kubernetes_sd_configs:
    #   - role: service
    #     namespaces:
    #       names: ["monitoring"]
    # relabel_configs:
    #   - source_labels: [__meta_kubernetes_service_name]
    #     action: keep
    #     regex: "threshold-exporter"
    #   - source_labels: [__meta_kubernetes_service_annotation_prometheus_io_port]
    #     action: replace
    #     target_label: __address__
    #     source_labels: [__address__, __meta_kubernetes_service_annotation_prometheus_io_port]
    #     regex: ([^:]+)(?::\d+)?;(\d+)
    #     replacement: $1:$2
```

> **提示**：`threshold-exporter` 以 HA ×2 副本部署，Service 會自動負載均衡。兩個副本吐出的指標內容完全一致（基於相同 ConfigMap），Prometheus 不論抓到哪個 Pod 都能取得完整的閾值集合。

### 驗證

```bash
# 1. 確認 target 狀態為 UP
curl -s 'http://<your-prometheus>:9090/api/v1/targets' \
  | jq '.data.activeTargets[] | select(.labels.job=="dynamic-thresholds") | {instance: .scrapeUrl, health: .health}'

# 預期：health: "up"

# 2. 確認閾值指標可查詢
curl -s 'http://<your-prometheus>:9090/api/v1/query?query=user_threshold_connections' \
  | jq '.data.result[] | {tenant: .metric.tenant, value: .value[1]}'

# 預期：每個 tenant 一筆，value 為其自訂閾值
# {"tenant": "db-a", "value": "100"}
# {"tenant": "db-b", "value": "80"}

# 3. 確認三態指標（state filter）
curl -s 'http://<your-prometheus>:9090/api/v1/query?query=user_threshold_state_filter' \
  | jq '.data.result[] | {tenant: .metric.tenant, metric_key: .metric.metric_key, value: .value[1]}'

# value=1 表示 custom，value=0 表示 default，value=-1 表示 disable
```

**✅ 通過條件**：`dynamic-thresholds` job 狀態為 `up`，且 `user_threshold_*` 指標可正常查詢。

---

## 步驟 3：掛載黃金規則包 (Rule Packs)

### 目標

將預寫好的 Recording Rule + Alert Rule 載入你的 Prometheus，實現動態閾值比對。

### 可用的規則包

| ConfigMap 名稱 | 內容 | 規則數 |
|----------------|------|--------|
| `prometheus-rules-mariadb` | `mariadb-recording.yml`, `mariadb-alert.yml` | 7R + 8A |
| `prometheus-rules-kubernetes` | `kubernetes-recording.yml`, `kubernetes-alert.yml` | 5R + 4A |
| `prometheus-rules-redis` | `redis-recording.yml`, `redis-alert.yml` | 7R + 6A |
| `prometheus-rules-mongodb` | `mongodb-recording.yml`, `mongodb-alert.yml` | 7R + 6A |
| `prometheus-rules-elasticsearch` | `elasticsearch-recording.yml`, `elasticsearch-alert.yml` | 7R + 7A |
| `prometheus-rules-platform` | `platform-alert.yml` | 0R + 4A |

> **你只需掛載與你環境相關的規則包。** 例如只用 MariaDB 和 Redis，就只掛這兩個。

### 設定：直接掛載 ConfigMap

將規則包 ConfigMap 掛載到 Prometheus Pod，並在設定中宣告讀取路徑。

**Step 3a — 修改 Prometheus Deployment/StatefulSet**

在你的 Prometheus 的 `volumes` 區段加入 Projected Volume（或個別 Volume）：

```yaml
# Projected Volume（推薦：所有規則包合併到單一掛載點）
volumes:
  - name: dynamic-alert-rules
    projected:
      sources:
        - configMap:
            name: prometheus-rules-mariadb
            optional: true                     # ← 規則包不存在時不影響 Prometheus 啟動
            items:
              - key: mariadb-recording.yml
                path: mariadb-recording.yml
              - key: mariadb-alert.yml
                path: mariadb-alert.yml
        - configMap:
            name: prometheus-rules-redis
            optional: true
            items:
              - key: redis-recording.yml
                path: redis-recording.yml
              - key: redis-alert.yml
                path: redis-alert.yml
        # ... 依需求加入其他規則包（kubernetes, mongodb, elasticsearch, platform）
```

在 Prometheus container 的 `volumeMounts` 加入：

```yaml
volumeMounts:
  - name: dynamic-alert-rules
    mountPath: /etc/prometheus/rules/dynamic-alerts
    readOnly: true
```

**Step 3b — 修改 prometheus.yml**

在 `rule_files` 中宣告新的規則目錄：

```yaml
rule_files:
  - "/etc/prometheus/rules/*.yml"                    # 你現有的規則（不要動）
  - "/etc/prometheus/rules/dynamic-alerts/*.yml"     # ★ 新增：動態閾值規則包
```

**Step 3c — 觸發 Prometheus 重新載入**

```bash
# 方法一：透過 lifecycle API（需啟用 --web.enable-lifecycle）
curl -X POST http://<your-prometheus>:9090/-/reload

# 方法二：送 SIGHUP
kill -HUP $(pidof prometheus)
```

### 驗證

```bash
# 1. 確認規則已載入
curl -s 'http://<your-prometheus>:9090/api/v1/rules' \
  | jq '.data.groups[].name' | sort -u

# 預期：應看到類似以下的規則群組名稱
# "mariadb-recording-rules"
# "mariadb-alert-rules"
# "redis-recording-rules"
# ...

# 2. 確認規則數量與預期一致
curl -s 'http://<your-prometheus>:9090/api/v1/rules' \
  | jq '[.data.groups[].rules[]] | length'

# 3. 確認沒有評估錯誤
curl -s 'http://<your-prometheus>:9090/api/v1/rules' \
  | jq '.data.groups[].rules[] | select(.lastError != "") | {name: .name, error: .lastError}'

# 預期：空輸出（沒有錯誤）

# 4. 確認 recording rule 有產生歸一化指標
curl -s 'http://<your-prometheus>:9090/api/v1/query?query=normalized_connections' \
  | jq '.data.result[] | {tenant: .metric.tenant, value: .value[1]}'
```

**✅ 通過條件**：規則群組全部載入、無評估錯誤、recording rule 正常產出歸一化指標。

---

## 端到端驗證 Checklist

完成上述三個步驟後，執行以下最終驗證：

```bash
# ① tenant 標籤存在於資料庫指標上
curl -s 'http://<your-prometheus>:9090/api/v1/query?query=up{job="tenant-db-exporters"}' \
  | jq '.data.result[] | .metric.tenant' | sort -u

# ② threshold-exporter 被正常抓取
curl -s 'http://<your-prometheus>:9090/api/v1/query?query=up{job="dynamic-thresholds"}' \
  | jq '(.data.result[0].value[1] == "1")'
# 預期：true

# ③ 向量匹配可正常運作（核心驗證）
curl -s 'http://<your-prometheus>:9090/api/v1/query?query=normalized_connections%20-%20on(tenant)%20user_threshold_connections' \
  | jq '.data.result[] | {tenant: .metric.tenant, diff: .value[1]}'
# 預期：每個 tenant 一筆結果。diff 為負表示在閾值內，為正表示超過閾值。
# 如果結果為空，代表 tenant 標籤匹配失敗 — 回頭檢查步驟 1。

# ④ Alert Rule 可正常評估
curl -s 'http://<your-prometheus>:9090/api/v1/rules?type=alert' \
  | jq '.data.groups[].rules[] | select(.name | startswith("MariaDB") or startswith("Redis")) | {name: .name, state: .state, activeCount: (.alerts | length)}'
```

> **排障**：如果步驟 ③ 返回空結果，最常見的原因是 `tenant` 標籤值不一致。用以下命令比對兩邊的值：
> ```bash
> # 資料庫指標的 tenant 值
> curl -s '...query=group(mysql_global_status_threads_connected) by (tenant)' | jq '.data.result[].metric.tenant'
> # 閾值指標的 tenant 值
> curl -s '...query=group(user_threshold_connections) by (tenant)' | jq '.data.result[].metric.tenant'
> # 兩邊的值必須完全一致（包含大小寫）
> ```

---

## 使用 da-tools CLI 快速驗證

如果你不想手動執行上述 curl 命令，可以使用我們打包好的 [da-tools CLI 容器](../components/da-tools/README.md)，一行指令完成驗證：

```bash
# 設定 Prometheus 位址
export PROM=http://prometheus.monitoring.svc.cluster.local:9090

# ① 確認特定 alert 的狀態
docker run --rm --network=host -e PROMETHEUS_URL=$PROM \
  ghcr.io/vencil/da-tools:0.2.0 check-alert MariaDBHighConnections db-a

# ② 觀測現有指標，取得閾值建議
docker run --rm --network=host -e PROMETHEUS_URL=$PROM \
  ghcr.io/vencil/da-tools:0.2.0 baseline --tenant db-a --duration 300

# ③ 啟動 Shadow Monitoring 雙軌比對
docker run --rm --network=host \
  -v $(pwd)/mapping.csv:/data/mapping.csv \
  -e PROMETHEUS_URL=$PROM \
  ghcr.io/vencil/da-tools:0.2.0 validate --mapping /data/mapping.csv --watch --rounds 5
```

> **提示**：`da-tools` 不需要 clone 整個專案，只需 `docker pull` 即可使用。詳見 [da-tools README](../components/da-tools/README.md)。

---

## 進階：與 Thanos / VictoriaMetrics 整合

本平台的規則包純粹基於標準 PromQL，因此與 Thanos 和 VictoriaMetrics 完全相容：

**Thanos**：規則包可載入 Thanos Ruler。確保 Thanos Querier 能同時查詢到 tenant 指標和 `threshold-exporter` 指標（兩個 StoreAPI 都要註冊）。

**VictoriaMetrics**：使用 vmalert 載入規則包。閾值指標透過 VMAgent 的 `scrape_configs` 抓取（設定方式與原生 Prometheus 相同）。

---

## Appendix：Prometheus Operator (kube-prometheus-stack) 整合

如果你的叢集使用 Prometheus Operator，以下是上述三個步驟的等價 CRD 設定：

### A1. 注入 tenant 標籤 — ServiceMonitor

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: tenant-db-exporters
  namespace: monitoring
  labels:
    release: kube-prometheus-stack       # ← 依你的 Operator 設定調整
spec:
  namespaceSelector:
    matchNames:
      - db-a
      - db-b
      # 新增 tenant namespace...
  selector:
    matchLabels:
      prometheus.io/scrape: "true"
  endpoints:
    - port: metrics                       # ← 依你的 exporter Service 定義
      interval: 10s
      relabelings:
        # ★ 將 namespace 注入為 tenant 標籤
        - sourceLabels: [__meta_kubernetes_namespace]
          targetLabel: tenant
```

### A2. 抓取 threshold-exporter — ServiceMonitor

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: dynamic-thresholds
  namespace: monitoring
  labels:
    release: kube-prometheus-stack
spec:
  namespaceSelector:
    matchNames: ["monitoring"]
  selector:
    matchLabels:
      app: threshold-exporter
  endpoints:
    - port: http-metrics                  # ← threshold-exporter Service 的 port 名稱
      interval: 15s
```

### A3. 掛載規則包 — PrometheusRule

每個 Rule Pack 對應一個 PrometheusRule CRD。以 MariaDB 為例：

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: dynamic-alerts-mariadb
  namespace: monitoring
  labels:
    release: kube-prometheus-stack       # ← Operator 的 ruleSelector 必須匹配此 label
spec:
  groups:
    # 將 configmap-rules-mariadb.yaml 中的 groups 內容貼入此處
    - name: mariadb-recording-rules
      rules: [...]                        # ← 從 rule-packs/ 目錄取得
    - name: mariadb-alert-rules
      rules: [...]
```

> **提示**：你可以使用 `kubectl get configmap prometheus-rules-mariadb -n monitoring -o jsonpath='{.data}'` 取得規則內容，再轉換為 PrometheusRule 格式。

### Operator 驗證

```bash
# 確認 ServiceMonitor 被 Operator 發現
kubectl get servicemonitor -n monitoring

# 確認 PrometheusRule 被載入
kubectl get prometheusrule -n monitoring

# 確認 Prometheus 的 config 已包含新的 scrape job
kubectl exec -n monitoring prometheus-kube-prometheus-stack-prometheus-0 -- \
  cat /etc/prometheus/config_out/prometheus.env.yaml | grep "dynamic-thresholds"
```

---

## 常見問題

**Q: 整合後，我需要重啟 Prometheus 嗎？**
A: 不需要。如果你啟用了 `--web.enable-lifecycle`，`curl -X POST /-/reload` 即可熱載入。ConfigMap 的變更也會由 Kubelet 自動同步到 Pod 的掛載路徑（通常延遲 1–2 分鐘）。

**Q: 我可以只掛載部分規則包嗎？**
A: 可以。所有規則包使用 `optional: true`，你只需加入你需要的。未掛載的規則包不會影響 Prometheus。

**Q: 我現有的 alerting rule 會衝突嗎？**
A: 不會。動態閾值規則包使用獨立的指標命名空間（`user_threshold_*`、`normalized_*`），不會與你現有的規則衝突。但建議在 Shadow Monitoring 階段（參考 [Shadow Monitoring SOP](shadow-monitoring-sop.md)）雙軌並行一段時間再切換。

**Q: threshold-exporter 需要部署在我的叢集裡嗎？**
A: 是的。`threshold-exporter` 需要存取 tenant ConfigMap，因此必須部署在同一叢集的 `monitoring` namespace。它是一個輕量的 Go binary，HA ×2 副本，資源消耗極低（< 50MB RSS）。

**Q: 如果我用的是 Thanos 多叢集架構怎麼辦？**
A: `threshold-exporter` 部署在資料叢集內（靠近 tenant ConfigMap）。Thanos Sidecar 會自動將閾值指標上傳到 Object Store。規則包載入到 Thanos Ruler，它會透過 Thanos Querier 進行跨叢集的向量匹配。
