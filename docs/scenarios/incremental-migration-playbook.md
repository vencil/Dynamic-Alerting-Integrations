---
title: "場景：漸進式遷移 Playbook"
tags: [scenario, migration, adoption, playbook]
audience: [platform-engineer, sre]
version: v2.2.0
lang: zh
---

# 場景：漸進式遷移 Playbook

> **v2.2.0** | 相關文件：[`migration-guide.md`](../migration-guide.md)、[`shadow-monitoring-cutover.md`](shadow-monitoring-cutover.md)、[`architecture-and-design.md` §2](../architecture-and-design.md)

## 概述

本 Playbook 指引企業從現有的混亂 Prometheus + Alertmanager 部署漸進式遷移至 Dynamic Alerting 平台，**零停機時間**。核心原則是 **Strangler Fig Pattern**：在既有系統上方建構一層乾淨的覆蓋層，逐步取代舊架構，不必先清理底層。

每個階段都是**獨立有價值的**——企業可以在任何階段停止，無需擔心系統癱瘓。遷移的速度完全由你掌控。

## 前置條件

- 運行中的 Prometheus 實例（`http://prometheus:9090`）
- 運行中的 Alertmanager（`http://alertmanager:9093`）
- Kubernetes 叢集（Kind、EKS、GKE 均可）
- `da-tools` 映像已推送至私有 registry 或可公開存取（`ghcr.io/vencil/da-tools:v2.1.0`）
- 叢集中至少有一個命名空間用於監控（如 `monitoring`、`observability`）

## 遷移時間表（典型案例）

| 階段 | 工作量 | 風險 | 時間 |
|------|--------|------|------|
| 階段 0：審計與評估 | 1 人日 | 零 | 1 天 |
| 階段 1：試點域部署 | 2 人日 | 低 | 3-5 天 |
| 階段 2：雙軌並行驗證 | 1 人日（監控）| 低 | 1-2 週 |
| 階段 3：切換 | 0.5 人日 | 低 | 4 小時 |
| 階段 4：擴展與清理 | 1 人日 × N 個域 | 低 | 每個域 2-3 週 |
| **總計（5 個域）** | **～15 人日** | **低** | **2-3 個月** |

---

## 階段 0：審計與評估（零風險評估）

**目標**：在不改變任何現存配置的情況下，理解你目前的監控體系。本階段是**唯讀**的，完全無風險。

### 步驟 0.1：分析現有 Alertmanager 配置

執行命令分析現有的 Alertmanager 路由樹、receiver 數量，識別是否已有租戶相關標籤：

```bash
da-tools onboard \
  --alertmanager-config alertmanager.yaml \
  --output audit-report.json
```

**預期輸出範例**（`audit-report.json`）：

```json
{
  "alertmanager_version": "0.25.0",
  "global": {
    "slack_api_url": "https://hooks.slack.com/services/T/B/c",
    "pagerduty_service_key": "pkey_xxx"
  },
  "receivers": [
    {
      "name": "default",
      "slack_configs": [{"channel": "#alerts"}],
      "pagerduty_configs": [{"service_key": "pkey_yyy"}]
    },
    {
      "name": "database-team",
      "slack_configs": [{"channel": "#db-alerts"}]
    },
    {
      "name": "backend-ops",
      "slack_configs": [{"channel": "#backend"}],
      "email_configs": [{"to": "ops@example.com"}]
    }
  ],
  "routes": [
    {
      "receiver": "default",
      "group_wait": "10s",
      "group_interval": "10s",
      "repeat_interval": "4h",
      "matchers": []
    },
    {
      "receiver": "database-team",
      "group_wait": "10s",
      "repeat_interval": "2h",
      "matchers": [
        {"name": "job", "value": "mariadb"}
      ]
    },
    {
      "receiver": "backend-ops",
      "group_wait": "30s",
      "repeat_interval": "6h",
      "matchers": [
        {"name": "job", "value": "~app-.*"}
      ]
    }
  ],
  "inhibit_rules": [
    {
      "source_matchers": [{"name": "severity", "value": "critical"}],
      "target_matchers": [{"name": "severity", "value": "warning"}],
      "equal": ["alertname", "instance"]
    }
  ],
  "recommendations": [
    "檢測到 3 個 receiver。建議將其對應至 3 個獨立租戶（redis-prod, mariadb-prod, app-team）",
    "未檢測到租戶相關標籤（tenant, owner, db）。建議在 Recording Rules 層添加"
  ]
}
```

**分析要點**：
- Receiver 數量 → 潛在租戶數量
- 現有 group_wait / repeat_interval → 後續 Dynamic Alerting 的 Routing Guardrails 參考值
- Inhibit rules → 是否需要遷移至 Dynamic Alerting 的 severity dedup 機制

### 步驟 0.2：分析現有 Prometheus 告警規則

分析現有規則，按類型分類（Recording Rules / Alerting Rules），識別遷移候選：

```bash
da-tools onboard \
  --prometheus-rules prometheus-rules.yaml \
  --prometheus-rules /etc/prometheus/rules.d/*.yaml \
  --output rule-audit.json
```

**預期輸出範例**（`rule-audit.json`）：

```json
{
  "summary": {
    "total_rules": 127,
    "recording_rules": 34,
    "alerting_rules": 93
  },
  "recording_rules": [
    {
      "name": "redis:memory:usage_percent",
      "group": "redis.yaml",
      "interval": "15s",
      "expression": "100 * redis_memory_used_bytes / redis_memory_max_bytes",
      "migration_priority": "high",
      "reason": "基礎指標，容易對應至 Rule Pack"
    }
  ],
  "alerting_rules": [
    {
      "name": "RedisHighMemory",
      "group": "redis.yaml",
      "for": "5m",
      "expression": "redis:memory:usage_percent > 85",
      "labels": {"severity": "warning"},
      "annotations": {"summary": "Redis memory > 85%"},
      "migration_priority": "high",
      "rule_pack_equivalent": "rule-pack-redis.yaml::RedisHighMemory",
      "notes": "完全對應 Rule Pack，建議優先遷移"
    },
    {
      "name": "AppCustomMetricA",
      "group": "custom.yaml",
      "expression": "custom_app_metric > 42",
      "migration_priority": "low",
      "reason": "自定義業務指標，暫無 Rule Pack 對應。建議後續維護在現有配置中"
    }
  ],
  "recommendations": [
    "High Priority（8 條）：Redis、MariaDB、JVM — 建議優先遷移",
    "Custom（15 條）：業務特定規則 — 建議 Phase 4 與 Platform Rule Pack 合併維護"
  ]
}
```

### 步驟 0.3：掃描叢集中的現有告警活動

掃描 Prometheus 中所有活躍的 scrape targets，了解實際監控的內容：

```bash
da-tools blind-spot \
  --config-dir /dev/null \
  --prometheus http://prometheus:9090 \
  --json \
  > blind-spot-report.json
```

**預期輸出範例**（`blind-spot-report.json`）：

```json
{
  "scrape_configs": [
    {
      "job_name": "prometheus",
      "count": 1,
      "targets": ["localhost:9090"]
    },
    {
      "job_name": "redis",
      "count": 3,
      "targets": [
        "redis-0:6379",
        "redis-1:6379",
        "redis-2:6379"
      ]
    },
    {
      "job_name": "mariadb",
      "count": 2,
      "targets": [
        "db-primary:3306",
        "db-replica:3306"
      ]
    }
  ],
  "available_databases": [
    {
      "db_type": "redis",
      "job_name": "redis",
      "instance_count": 3,
      "rule_pack_available": "rule-pack-redis.yaml",
      "recommendation": "✓ 可直接使用 Rule Pack"
    },
    {
      "db_type": "mariadb",
      "job_name": "mariadb",
      "instance_count": 2,
      "rule_pack_available": "rule-pack-mariadb.yaml",
      "recommendation": "✓ 可直接使用 Rule Pack"
    }
  ]
}
```

### 步驟 0.4：決策矩陣 — 選擇試點域

基於 Phase 0.1-0.3 的輸出，填寫以下決策矩陣，選擇試點域（通常是指標最「乾淨」或痛點最明顯的域）：

```yaml
# decision-matrix.yaml
candidates:
  redis-prod:
    metrics_cleanliness: 9/10  # 指標命名標準
    rule_pack_coverage: 9/10   # Rule Pack 覆蓋度
    pain_points: "告警噪音，誤報率 15%"
    team_readiness: "高"
    recommendation: "✓ PRIMARY CHOICE"
    migration_effort: "低"

  mariadb-prod:
    metrics_cleanliness: 7/10
    rule_pack_coverage: 8/10
    pain_points: "告警延遲 >10min，影響 RTO"
    team_readiness: "中"
    recommendation: "✓ SECONDARY CHOICE"
    migration_effort: "低"

  kafka-prod:
    metrics_cleanliness: 6/10
    rule_pack_coverage: 7/10
    pain_points: "告警分組混亂，難以追蹤"
    team_readiness: "中"
    recommendation: "◯ Phase 2 之後"
    migration_effort: "中"

  custom-app:
    metrics_cleanliness: 3/10
    rule_pack_coverage: 1/10
    pain_points: "自定義業務規則，無法標準化"
    team_readiness: "低"
    recommendation: "✗ Phase 4 最後遷移"
    migration_effort: "高"
```

**選擇試點域的建議**：
- 優先選擇 **Rule Pack 覆蓋度 >= 8/10** 的域（如 Redis、MariaDB）
- 避免在初期選擇高度自定義的業務規則
- 優先選擇 **痛點明顯**（噪音、延遲、分組混亂）的域，以便快速展示價值

### 階段 0 回滾

無需回滾。本階段是唯讀的，不涉及任何系統改變。

---

## 階段 1：試點域部署（單一領域試點）

**目標**：為選定的單一域（如 Redis）在 Dynamic Alerting 平台部署，**以影子模式**併行於現有告警。新告警被發出但暫不路由至任何 receiver。

### 步驟 1.1：生成租戶配置

基於 Phase 0 的決策，使用 `scaffold` 命令生成初始配置：

```bash
mkdir -p conf.d/

da-tools scaffold \
  --tenant redis-prod \
  --db redis \
  --non-interactive \
  --output conf.d/redis-prod.yaml
```

**預期輸出**（`conf.d/redis-prod.yaml`）：

```yaml
tenants:
  redis-prod:
    tier: standard
    db: redis

    # Recording Rules 配置
    recording_rules:
      enabled: true
      rule_pack: rule-pack-redis.yaml
      cardinality_limit: 500
      scrape_interval: 15s

    # Threshold 配置（初始保守值）
    thresholds:
      memory:
        warning: 75
        critical: 90
      connections:
        warning: 1000
        critical: 5000
      evictions:
        warning: 10
        critical: 100

    # 路由配置（初始禁用）
    _routing:
      enabled: false
      receiver:
        type: slack
        api_url: "https://hooks.slack.com/services/CHANGE_ME"
        channel: "#redis-alerts"
```

### 步驟 1.2：編輯閾值配置

基於 Phase 0.2 的審計輸出，調整 threshold 參數以符合現有規則的邏輯。**重點是保守設置**，寧願在 Phase 2 收集數據後再調整：

```bash
# 編輯閾值以匹配現有規則
cat >> conf.d/redis-prod.yaml << 'EOF'

    thresholds:
      memory:
        # 現有規則：redis:memory:usage_percent > 85 → warning
        warning: 75    # 稍保守一些，給予調整空間
        critical: 90   # 對齊現有 critical 規則

      connections:
        # 基於審計結果
        warning: 800
        critical: 3000

      evictions_rate:
        warning: 5
        critical: 50
EOF
```

### 步驟 1.3：部署 threshold-exporter

在試點環境中部署 threshold-exporter，掛載 conf.d/ 目錄：

```bash
# 假設使用 Helm
helm repo add vencil https://ghcr.io/vencil/charts
helm repo update

helm install threshold-exporter-redis vencil/threshold-exporter \
  --namespace monitoring \
  --set image.tag=v2.2.0 \
  --set config.dir=/etc/threshold-exporter/conf.d \
  --set replicaCount=2 \
  -f - << 'EOF'
extraVolumes:
  - name: config
    configMap:
      name: threshold-exporter-config-redis
extraVolumeMounts:
  - name: config
    mountPath: /etc/threshold-exporter/conf.d
EOF

# 先創建 ConfigMap
kubectl create configmap threshold-exporter-config-redis \
  --from-file=conf.d/redis-prod.yaml \
  -n monitoring \
  --dry-run=client -o yaml | kubectl apply -f -
```

### 步驟 1.4：驗證 Metrics 發出

查詢 threshold-exporter 發出的 metrics：

```bash
# Port-forward（如果需要）
kubectl port-forward -n monitoring \
  svc/threshold-exporter-redis 8080:8080 &

# 查詢 metrics
curl http://localhost:8080/metrics | grep redis_user_threshold

# 預期輸出
redis_user_threshold_memory_warning{tenant="redis-prod"} 75
redis_user_threshold_memory_critical{tenant="redis-prod"} 90
redis_user_threshold_connections_warning{tenant="redis-prod"} 800
redis_user_threshold_connections_critical{tenant="redis-prod"} 3000
```

### 步驟 1.5：掛載 Rule Pack

創建包含 Rule Pack 的 ConfigMap，掛載至 Prometheus：

```bash
# 從 Platform 規則庫提取 Redis Rule Pack
curl -o rule-pack-redis.yaml \
  https://raw.githubusercontent.com/vencil/vibe-k8s-lab/main/rule-packs/rule-pack-redis.yaml

# 創建 ConfigMap
kubectl create configmap rule-pack-redis \
  --from-file=rule-pack-redis.yaml \
  -n monitoring \
  --dry-run=client -o yaml | kubectl apply -f -

# 更新 Prometheus 配置以掛載此 ConfigMap
kubectl patch cm prometheus-config -n monitoring --type merge -p '{
  "data": {
    "prometheus.yaml": "global:\n  scrape_interval: 15s\nrule_files:\n  - /etc/prometheus/rules/rule-pack-redis.yaml\nscrape_configs:\n  - job_name: prometheus\n    static_configs:\n      - targets: [localhost:9090]\n"
  }
}'

# 重啟 Prometheus 以載入新規則
kubectl rollout restart deployment/prometheus -n monitoring

# 驗證規則已加載
kubectl logs -n monitoring deployment/prometheus -f --tail=50 | grep "rule-pack-redis"
```

### 步驟 1.6：驗證 Recording Rules

等待 Prometheus 完成規則加載和首次評估（通常 15-30 秒），然後驗證：

```bash
# Port-forward Prometheus
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &

# 查詢 recording rule 的輸出
curl 'http://localhost:9090/api/v1/query?query=redis:memory:usage_percent'

# 預期輸出
{
  "status": "success",
  "data": {
    "resultType": "vector",
    "result": [
      {
        "metric": {"__name__": "redis:memory:usage_percent", "instance": "redis-0:6379"},
        "value": [1710796200, "42.5"]
      }
    ]
  }
}

# 檢查告警規則是否已觸發（不應在 Phase 1 出現告警，除非實際閾值被突破）
curl 'http://localhost:9090/api/v1/query?query=ALERTS{alertname="RedisHighMemory"}'
```

### 步驟 1.7：驗證告警未被路由

確認新的告警已被 Prometheus 產生，但尚未被 Alertmanager 路由到任何 receiver：

```bash
# 查看 Prometheus 中活躍的告警
curl 'http://localhost:9090/api/v1/alerts' | jq '.data.alerts[] | select(.labels.alertname=="RedisHighMemory")'

# 預期：即使有告警觸發，Alertmanager 中也應該沒有對應的分組
# （因為我們在 Phase 2 才添加路由）

kubectl port-forward -n monitoring svc/alertmanager 9093:9093 &
curl 'http://localhost:9093/api/v1/alerts' | jq '.[].alerts[] | select(.labels.alertname=="RedisHighMemory")'

# 預期輸出：空（或未出現 RedisHighMemory）
```

### 階段 1 驗證清單

- [ ] threshold-exporter 部署成功，2 個 Pod 運行中
- [ ] metrics 查詢可得到 `redis_user_threshold_*` 系列指標
- [ ] Rule Pack 已掛載，Prometheus 日誌無錯誤
- [ ] Recording Rules 產生輸出（`redis:memory:usage_percent` 等）
- [ ] Alerting Rules 產生（在 Prometheus 中可見），但未被路由至 Alertmanager receiver

### 階段 1 回滾

若需回滾（例如遇到意外問題），執行以下步驟：

```bash
# 1. 刪除 threshold-exporter 部署
helm uninstall threshold-exporter-redis -n monitoring

# 2. 刪除 Rule Pack ConfigMap
kubectl delete cm rule-pack-redis -n monitoring

# 3. 恢復 Prometheus 配置（移除 rule-pack-redis.yaml 挂載）
kubectl patch cm prometheus-config -n monitoring --type merge -p '{
  "data": {
    "prometheus.yaml": "... 原始配置 ..."
  }
}'

# 4. 重啟 Prometheus
kubectl rollout restart deployment/prometheus -n monitoring

# 驗證回滾完成
kubectl get pods -n monitoring
```

**回滾後**：系統恢復至審計前狀態，現有告警繼續正常運作。

---

## 階段 2：雙軌並行驗證（雙軌並行驗證）

**目標**：新舊告警同時運作，比較品質。使用 1-2 週時間收集數據，驗證 Dynamic Alerting 的告警品質不低於現有系統。

### 步驟 2.1：生成 Alertmanager 路由片段

使用 `generate-routes` 命令為試點租戶生成 Alertmanager 路由配置：

```bash
da-tools generate-routes \
  --config-dir conf.d/ \
  --tenant redis-prod \
  --output alertmanager-fragment.yaml
```

**預期輸出**（`alertmanager-fragment.yaml`）：

```yaml
# 新增路由（插入至現有配置頂部）
route:
  receiver: alertmanager-default
  routes:
    # ========== Dynamic Alerting 試點路由 ==========
    - receiver: da-pilot-slack
      match:
        da_managed: "true"
        tenant: redis-prod
      group_wait: 5s
      group_interval: 5m
      repeat_interval: 4h
      continue: false
    # ========== 現有路由（保持不變）==========
    - receiver: database-team
      match:
        job: mariadb
      group_wait: 10s
      group_interval: 10s
      repeat_interval: 2h
    # ... 其他現有路由
```

### 步驟 2.2：準備雙軌配置

備份現有 Alertmanager 配置，然後在頂部插入新路由：

```bash
# 備份
cp alertmanager.yaml alertmanager.yaml.backup-phase1

# 合併配置
cat > alertmanager-patch.yaml << 'EOF'
global:
  slack_api_url: "https://hooks.slack.com/services/T/B/c"

receivers:
  # ===== 新增 receiver（用於試點） =====
  - name: da-pilot-slack
    slack_configs:
      - api_url: "https://hooks.slack.com/services/T/B/d"  # 不同的 Slack channel
        channel: "#da-pilot-redis"
        title: "[DA PILOT] {{ .GroupLabels.alertname }}"
        text: "Tenant: {{ .GroupLabels.tenant }} | Severity: {{ .GroupLabels.severity }}"

  # ===== 現有 receiver（保持不變）=====
  - name: default
    slack_configs:
      - api_url: "https://hooks.slack.com/services/T/B/c"
        channel: "#alerts"

route:
  receiver: default
  # ===== 新增路由（優先級最高）=====
  routes:
    - receiver: da-pilot-slack
      match:
        da_managed: "true"
        tenant: redis-prod
      group_wait: 5s
      group_interval: 5m
      repeat_interval: 4h
      continue: true  # 允許繼續匹配後續路由（雙軌記錄）

  # ===== 現有路由（保持不變）=====
  - receiver: database-team
    match_re:
      job: ".*database.*"
    group_wait: 10s
    repeat_interval: 2h

inhibit_rules:
  # 現有的 inhibit rule
  - source_matchers:
      - severity: critical
    target_matchers:
      - severity: warning
    equal: [alertname, instance]
EOF

# 使用 kubectl patch 應用配置（避免 cat << EOF）
kubectl create configmap alertmanager-config-phase2 \
  --from-file=alertmanager-patch.yaml \
  -n monitoring \
  --dry-run=client -o yaml | kubectl apply -f -

# 更新 Alertmanager
kubectl set env deployment/alertmanager \
  -n monitoring \
  ALERTMANAGER_CONFIG_RELOAD="true"
```

### 步驟 2.3：預檢查（Shadow Verify Preflight）

運行預檢查，確保雙軌配置合理：

```bash
# 準備 shadow mapping 文件（將新舊告警對應）
cat > shadow-mapping.yaml << 'EOF'
mappings:
  - old_alert: "RedisHighMemory"
    new_alert: "RedisHighMemory"
    comment: "相同告警名稱，預期行為一致"

  - old_alert: "RedisHighConnections"
    new_alert: "RedisHighConnections"
    comment: "新 Rule Pack 的對應告警"

  - old_alert: "RedisEvictions"
    new_alert: "RedisHighEvictionRate"
    comment: "新規則使用更精確的名稱"
EOF

# 執行預檢查
da-tools shadow-verify preflight \
  --mapping shadow-mapping.yaml \
  --config-dir conf.d/ \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093
```

**預期輸出**：

```
✓ Alertmanager 配置語法正確
✓ Route 優先級：da-pilot-slack (優先) > database-team
✓ 映射覆蓋率：3/3 告警已映射
⚠ 警告：repeat_interval 在 da-pilot-slack (4h) 與 database-team (2h) 不同
  → 建議一致，或添加 `continue: true` 以防止誤路由
✓ Pre-flight 檢查通過
```

### 步驟 2.4：監控雙軌運行（1-2 週）

讓系統並行運作 1-2 週，期間實時觀察兩個 Slack channels 中的告警：

```bash
# 每天運行一次質量評估
da-tools alert-quality \
  --prometheus http://prometheus:9090 \
  --tenant redis-prod \
  --lookback 24h \
  --json \
  > alert-quality-$(date +%Y-%m-%d).json
```

**預期輸出範例**（`alert-quality-2026-03-25.json`）：

```json
{
  "date": "2026-03-25",
  "tenant": "redis-prod",
  "period_hours": 24,
  "metrics": {
    "old_alerts": {
      "total_fired": 12,
      "false_positives": 2,
      "mean_latency_sec": 180,
      "mean_duration_min": 8,
      "total_notifications": 24
    },
    "new_alerts": {
      "total_fired": 12,
      "false_positives": 0,
      "mean_latency_sec": 45,
      "mean_duration_min": 5,
      "total_notifications": 12
    },
    "quality_delta": {
      "false_positive_reduction": "100%",
      "latency_improvement": "75%",
      "notification_reduction": "50%",
      "overall_score": "A+"
    }
  },
  "observations": [
    "新告警更及時（45s vs 180s）",
    "誤報從 2 降至 0",
    "告警分組更好，總通知數從 24 降至 12"
  ]
}
```

### 步驟 2.5：匯總與決策

在 1-2 週後，匯總數據並決策是否進行切換：

```bash
# 匯總週期內所有日報
cat alert-quality-*.json | jq -s '
  {
    period: "2026-03-18 to 2026-03-25",
    old_avg_latency_sec: (map(.metrics.old_alerts.mean_latency_sec) | add / length),
    new_avg_latency_sec: (map(.metrics.new_alerts.mean_latency_sec) | add / length),
    old_avg_fps: (map(.metrics.old_alerts.false_positives) | add / length),
    new_avg_fps: (map(.metrics.new_alerts.false_positives) | add / length),
    improvement_summary: "延遲下降 X%，誤報下降 Y%"
  }
'
```

**決策準則**：
- **新告警延遲 < 舊告警延遲** → Go（通常 75% 以上改進）
- **新告警誤報率 <= 舊告警誤報率** → Go
- **新告警分組 > 舊告警分組** → 更好的可觀測性 → Go

若三個條件均滿足，進行階段 3 切換。若有疑慮，延長雙軌時間或回滾。

### 階段 2 回滾

若雙軌驗證失敗，恢復至階段 1 結束狀態：

```bash
# 1. 移除新路由（回滾 Alertmanager 配置）
kubectl patch cm alertmanager-config -n monitoring \
  --type merge -p '{"data": {"alertmanager.yaml": "... 原始配置 ..."}}'

# 2. 重啟 Alertmanager
kubectl rollout restart deployment/alertmanager -n monitoring

# 3. 驗證舊告警恢復
sleep 30
curl http://localhost:9093/api/v1/alerts | jq 'length'
```

---

## 階段 3：切換（切換）

**目標**：禁用試點域的舊告警規則，使 Dynamic Alerting 成為主告警來源。系統無中斷。

### 步驟 3.1：乾跑切換預演

在實際執行前，預演一遍切換過程，確保無誤：

```bash
da-tools cutover \
  --tenant redis-prod \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093 \
  --dry-run \
  --verbose
```

**預期輸出**：

```
========== Cutover Dry-Run Report ==========
Tenant: redis-prod
Current State:
  - Recording Rules: ACTIVE (redis:memory:usage_percent, etc.)
  - Alerting Rules (Old): ACTIVE (RedisHighMemory, RedisHighConnections)
  - Dynamic Alerting: ACTIVE

Planned Actions:
  1. Keep Recording Rules: redis:* (保持，供 Dynamic Alerting 使用)
  2. Disable Old Rules: prometheus.yaml::RedisHighMemory, etc.
  3. Update Alertmanager route: remove continue: true, finalize da-pilot-slack as primary
  4. Remove shadow labels: strip da_managed marker

Expected Result:
  - Recording Rules: ACTIVE
  - Old Alerting Rules: DISABLED
  - Dynamic Alerting: ACTIVE (primary)
  - Alertmanager routing: redis-prod → da-pilot-slack (only)

Health Checks:
  ✓ No orphaned rules detected
  ✓ Recording rules will still evaluate
  ✓ Failover path verified

Rollback Command (if needed):
  da-tools cutover --tenant redis-prod --rollback
```

**驗證乾跑輸出**：
- 確認只有舊 Alerting Rules 被禁用，Recording Rules 保持啟用
- 確認 Alertmanager 路由最終指向 `da-pilot-slack` 且不會重複發送

### 步驟 3.2：執行切換

確認乾跑結果無誤，執行實際切換：

```bash
da-tools cutover \
  --tenant redis-prod \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093 \
  --execute
```

**步驟 3.2.1**：禁用舊告警規則

```bash
# 從 Prometheus 規則中移除或註釋舊的 Redis 告警
# 保留 Recording Rules（redis:memory:usage_percent 等），只移除 alert 部分

kubectl patch cm prometheus-rules-redis \
  -n monitoring \
  -p '{"data": {"old_rules_disabled": "true"}}'
```

**步驟 3.2.2**：更新 Alertmanager 路由

```bash
# 移除 `continue: true`，讓新路由成為唯一目標
kubectl patch cm alertmanager-config -n monitoring --type merge -p '{
  "data": {
    "alertmanager.yaml": "route:\n  receiver: default\n  routes:\n    - receiver: da-pilot-slack\n      match:\n        da_managed: \"true\"\n        tenant: redis-prod\n      group_wait: 5s\n      group_interval: 5m\n      repeat_interval: 4h\n      continue: false\n    # 其他路由保持不變\n"
  }
}'

# 重啟 Alertmanager
kubectl rollout restart deployment/alertmanager -n monitoring
```

### 步驟 3.3：全面健康檢查

切換完成後，執行完整診斷：

```bash
da-tools diagnose redis-prod \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093 \
  --verbose
```

**預期輸出**：

```
========== Diagnostic Report for redis-prod ==========

Recording Rules:
  ✓ redis:memory:usage_percent → 42.5 (healthy)
  ✓ redis:eviction:rate → 0.05/sec (healthy)
  ✓ redis:connections:active → 127 (healthy)

Alerting Rules (from Dynamic Alerting):
  ✓ RedisHighMemory (critical) → FIRING (as expected)
    Instance: redis-0:6379, Value: 92%, Latency: 15s
  ✓ RedisHighConnections (warning) → NOT FIRING (threshold: 800, actual: 127)
  ✓ RedisHighEvictionRate (critical) → NOT FIRING

Alertmanager Routes:
  ✓ redis-prod alerts routed to: da-pilot-slack
  ✓ Route priority: 1st (matched)
  ✓ No orphaned alerts detected

Receiver Health:
  ✓ da-pilot-slack: last webhook delivery 5s ago (success)
  ✓ Notification count (last 1h): 2 (expected)

Overall Health: GOOD
  - All rules evaluated successfully
  - Routing working as expected
  - Notifications delivered on time
```

### 步驟 3.4：確認舊告警已禁用

驗證舊告警不再被發出：

```bash
# 查詢 Prometheus，確認舊 alert 不在活躍告警中
curl 'http://prometheus:9090/api/v1/rules' | jq '
  .data.groups[]
  | select(.file | contains("redis"))
  | .rules[]
  | select(.type == "alert")
  | {name: .name, state: .state}
'

# 預期輸出：空（或僅顯示 Dynamic Alerting 的告警，不顯示舊規則）
```

### 階段 3 驗證清單

- [ ] 乾跑預演成功，無警告
- [ ] 實際切換已執行，無錯誤
- [ ] 舊告警規則已禁用
- [ ] 新告警正常發出（Alertmanager 可見）
- [ ] 通知正確路由至 da-pilot-slack
- [ ] diagnose 報告顯示 GOOD
- [ ] 1 小時內未發生異常告警或通知延遲

### 階段 3 回滾

若切換後發生重大問題，執行回滾：

```bash
da-tools cutover \
  --tenant redis-prod \
  --rollback
```

此命令將：
1. 重新啟用舊告警規則
2. 恢復 Alertmanager 路由（重新添加 `continue: true`）
3. 驗證舊告警已恢復發出

**回滾後**：系統恢復至階段 2 結束狀態，雙軌並行運行。

---

## 階段 4：擴展與清理（擴展與清理）

**目標**：對剩餘域重複階段 1-3，最後進行系統級清理。

### 步驟 4.1：遷移下一個域（循環）

選擇下一個候選域（如 MariaDB），重複階段 1-3：

```bash
# Phase 1：部署
da-tools scaffold --tenant mariadb-prod --db mariadb --non-interactive \
  --output conf.d/mariadb-prod.yaml

# 編輯 conf.d/mariadb-prod.yaml，調整閾值（參考 Phase 0 審計）
# 部署 threshold-exporter、掛載 Rule Pack
helm install threshold-exporter-mariadb vibe/threshold-exporter \
  --namespace monitoring \
  -f conf.d/mariadb-prod.yaml

# Phase 2：雙軌驗證（1-2 週）
da-tools alert-quality --tenant mariadb-prod --lookback 168h

# Phase 3：切換
da-tools cutover --tenant mariadb-prod --execute
```

重複此過程直到所有候選域均已遷移。典型流程：

1. **Week 1-2**：Redis 試點（Phase 1）
2. **Week 2-4**：Redis 雙軌驗證（Phase 2）
3. **Week 4**：Redis 切換（Phase 3）
4. **Week 5-7**：MariaDB 試點 + 雙軌驗證
5. **Week 7**：MariaDB 切換
6. **Week 8-10**：Kafka 試點 + 雙軌驗證
7. **Week 10**：Kafka 切換
8. ...（重複）

### 步驟 4.2：全量驗證

所有域均遷移完成後，執行全量驗證：

```bash
# 驗證所有租戶配置
da-tools validate-config \
  --config-dir conf.d/ \
  --ci \
  --json \
  > validation-report.json

# 預期輸出
{
  "status": "PASS",
  "summary": {
    "total_tenants": 5,
    "valid": 5,
    "invalid": 0,
    "cardinality_violations": 0
  },
  "details": [
    {"tenant": "redis-prod", "status": "PASS", "rules": 8, "cardinality": 120},
    {"tenant": "mariadb-prod", "status": "PASS", "rules": 6, "cardinality": 95},
    ...
  ]
}
```

### 步驟 4.3：批量診斷

對所有租戶執行健康檢查：

```bash
da-tools batch-diagnose \
  --config-dir conf.d/ \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093 \
  --json \
  > batch-diagnose.json

# 預期：所有租戶 status = GOOD
jq '.results[] | {tenant: .tenant, status: .status}' batch-diagnose.json
```

### 步驟 4.4：清理遺留配置

移除不再需要的舊 Prometheus 規則和配置：

```bash
# 備份原始 Prometheus 規則檔
cp prometheus-rules.yaml prometheus-rules.yaml.backup-phase4

# 移除已遷移域的舊規則
grep -v -e "redis" -e "mariadb" -e "kafka" prometheus-rules.yaml \
  > prometheus-rules-cleaned.yaml

# 驗證移除結果
diff prometheus-rules.yaml prometheus-rules-cleaned.yaml

# 應用新配置
kubectl create configmap prometheus-rules-cleaned \
  --from-file=prometheus-rules-cleaned.yaml \
  -n monitoring \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl patch deployment prometheus -n monitoring --type merge -p \
  '{"spec": {"template": {"spec": {"containers": [{"name": "prometheus", "args": ["--config.file=/etc/prometheus/prometheus-cleaned.yaml"]}]}}}}'
```

### 步驟 4.5：清理測試租戶

如有測試或試驗租戶，移除：

```bash
# 列出所有租戶
da-tools ls --config-dir conf.d/

# 卸載不需要的租戶
da-tools offboard --tenant test-domain-1

# 驗證卸載
da-tools validate-config --config-dir conf.d/ --ci
```

### 步驟 4.6：更新文件與交接

更新內部文件，記錄遷移完成的各項細節：

```bash
# 編輯 migration-report.yaml
cat > migration-report.yaml << 'EOF'
migration_summary:
  start_date: 2026-03-18
  completion_date: 2026-05-20
  duration_weeks: 9

domains_migrated:
  - name: redis-prod
    phase_1_date: 2026-03-18
    phase_3_date: 2026-04-01
    quality_improvement: "75% latency reduction, 100% false positive elimination"

  - name: mariadb-prod
    phase_1_date: 2026-04-02
    phase_3_date: 2026-04-23
    quality_improvement: "60% latency reduction, alert grouping improved"

  - name: kafka-prod
    phase_1_date: 2026-04-24
    phase_3_date: 2026-05-20
    quality_improvement: "50% latency reduction"

legacy_rules_removed: 127
legacy_receivers_decommissioned: 3
new_recording_rules_added: 24
total_cardinality_reduction: "18%"

lessons_learned:
  - "選擇指標最乾淨的域作為試點，加速早期學習"
  - "雙軌驗證期間，主動與告警接收方溝通品質改進"
  - "Phase 2 延長至 2 週以上，能更充分地涵蓋多種告警場景"
EOF
```

---

## 常見問題（FAQ）

### Q1：遷移前需要清理 scrape 配置嗎？

**A**：不需要。Dynamic Alerting 的 Recording Rules（第 1 部分）在現有 scrape 配置之上創建一層乾淨的抽象。即使 scrape 配置混亂或不規範，Recording Rules 也能聚合、規範化，產生標準化的指標。

**建議**：遷移完成後的清理工作中，可以逐步改進 scrape 配置（例如統一標籤命名、移除重複 targets），但這不是遷移的前提條件。

### Q2：遷移中途某個域失敗了怎麼辦？

**A**：每個域都是獨立的。若 Redis 的切換失敗，只需回滾 Redis（`da-tools cutover --tenant redis-prod --rollback`），其他域（MariaDB、Kafka 等）不受影響，繼續正常運作。

回滾後，可重新評估問題（如閾值設置不當），修復後再次嘗試切換。

### Q3：整個遷移需要多長時間？

**A**：取決於域的數量和驗證嚴謹度：

- **Phase 0**（審計）：1 天
- **每個域的 Phase 1-3**：2-3 週（其中 Phase 2 雙軌驗證通常 1-2 週）
- **Phase 4**（清理）：2-3 天

**典型 5-域遷移時間線**：

| 階段 | 時間 |
|------|------|
| Phase 0（全局審計） | 1 天 |
| Phase 1-3（Redis） | 3 週 |
| Phase 1-3（MariaDB） | 2 週 |
| Phase 1-3（Kafka） | 2 週 |
| Phase 1-3（JVM） | 1.5 週 |
| Phase 1-3（自定義） | 2.5 週 |
| Phase 4（清理） | 2 天 |
| **總計** | **11 週（~2.5 個月）** |

---

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [遷移指南（工具級參考）](../migration-guide.md) | ⭐⭐⭐ |
| [場景：Shadow Monitoring 全自動切換工作流](shadow-monitoring-cutover.md) | ⭐⭐⭐ |
| [Architecture & Design §2.13 效能架構](../architecture-and-design.md) | ⭐⭐⭐ |
| [da-tools CLI Reference](../cli-reference.md) | ⭐⭐ |
| [場景：租戶完整生命週期管理](tenant-lifecycle.md) | ⭐⭐ |
| [場景：GitOps CI/CD 整合指南](gitops-ci-integration.md) | ⭐⭐ |
| [場景：Hands-on Lab 實戰教程](hands-on-lab.md) | ⭐⭐ |
| [Shadow Monitoring SRE SOP](../shadow-monitoring-sop.md) | ⭐ |
