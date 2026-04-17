---
title: "場景：漸進式遷移 Playbook"
tags: [scenario, migration, adoption, playbook]
audience: [platform-engineer, sre]
version: v2.7.0
lang: zh
---

# 場景：漸進式遷移 Playbook

> **v2.7.0** | 相關文件：[`migration-guide.md`](../migration-guide.md)、[`shadow-monitoring-cutover.md`](shadow-monitoring-cutover.md)、[`architecture-and-design.md` §2](../architecture-and-design.md)

## 概述

本 Playbook 指引企業從現有的混亂 Prometheus + Alertmanager 部署漸進式遷移至 Dynamic Alerting 平台，**零停機時間**。核心原則是 **Strangler Fig Pattern**：在既有系統上方建構一層乾淨的覆蓋層，逐步取代舊架構，不必先清理底層。

每個階段都是**獨立有價值的**——企業可以在任何階段停止，無需擔心系統癱瘓。遷移的速度完全由你掌控。

## 前置條件

- 運行中的 Prometheus 實例（`http://prometheus:9090`）
- 運行中的 Alertmanager（`http://alertmanager:9093`）
- Kubernetes 叢集（Kind、EKS、GKE 均可）
- `da-tools` 映像已推送至私有 registry 或可公開存取（`ghcr.io/vencil/da-tools:v2.7.0`）
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

### Step 0.1: Analyze Existing Alertmanager Configuration

執行命令分析現有的 Alertmanager 路由樹、receiver 數量，識別是否已有租戶相關標籤：

```bash
da-tools onboard \
  --alertmanager-config alertmanager.yaml \
  --output audit-report.json
```

**預期輸出**：`audit-report.json` 包含 Alertmanager 版本、全局設定、receiver 列表（名稱、通知渠道）、路由樹結構、inhibit rules、以及遷移建議。分析要點：
- Receiver 數量 → 潛在租戶數量
- 現有 group_wait / repeat_interval → 後續 Dynamic Alerting 的 Routing Guardrails 參考值
- Inhibit rules → 是否需要遷移至 Dynamic Alerting 的 severity dedup 機制

### Step 0.2: Analyze Existing Prometheus Alert Rules

分析現有規則，按類型分類（Recording Rules / Alerting Rules），識別遷移候選：

```bash
da-tools onboard \
  --prometheus-rules prometheus-rules.yaml \
  --prometheus-rules /etc/prometheus/rules.d/*.yaml \
  --output rule-audit.json
```

**預期輸出**：`rule-audit.json` 匯總告警規則統計、逐條遷移優先級評分、rule-pack 對應建議。建議優先遷移高優先級規則（如 Redis、MariaDB 相關），延後自定義業務規則。

### 步驟 0.3：掃描叢集中的現有告警活動

掃描 Prometheus 中所有活躍的 scrape targets，了解實際監控的內容：

```bash
da-tools blind-spot \
  --config-dir /dev/null \
  --prometheus http://prometheus:9090 \
  --json \
  > blind-spot-report.json
```

**預期輸出**：`blind-spot-report.json` 列舉 scrape targets、已有 rule-pack 覆蓋的數據庫類型、推薦直接使用的 Rule Pack。

### 步驟 0.4：決策矩陣 — 選擇試點域

基於 Phase 0.1-0.3 的輸出，填寫以下決策矩陣，選擇試點域（通常是指標最「乾淨」或痛點最明顯的域）：

```yaml
candidates:
  redis-prod:
    metrics_cleanliness: 9/10
    rule_pack_coverage: 9/10
    pain_points: "告警噪音，誤報率 15%"
    team_readiness: "高"
    recommendation: "✓ PRIMARY CHOICE"

  mariadb-prod:
    metrics_cleanliness: 7/10
    rule_pack_coverage: 8/10
    pain_points: "告警延遲 >10min，影響 RTO"
    recommendation: "✓ SECONDARY CHOICE"

  custom-app:
    metrics_cleanliness: 3/10
    rule_pack_coverage: 1/10
    recommendation: "✗ Phase 4 最後遷移"
```

**選擇試點域的建議**：優先選擇 Rule Pack 覆蓋度 >= 8/10 的域，避免初期選擇高度自定義的業務規則，優先選擇痛點明顯的域以快速展示價值。

### 階段 0 回滾

無需回滾。本階段是唯讀的，不涉及任何系統改變。

---

## 階段 1：試點域部署（單一領域試點）

**目標**：為選定的單一域（如 Redis）在 Dynamic Alerting 平台部署，以影子模式併行於現有告警。新告警被發出但暫不路由至任何 receiver。

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

**預期輸出**：`conf.d/redis-prod.yaml` 包含 recording rules 設定、threshold 初始值（conservative）、路由配置（初始禁用）。

### 步驟 1.2：編輯閾值配置

基於 Phase 0.2 的審計輸出，調整 threshold 參數以符合現有規則的邏輯。**重點是保守設置**，寧願在 Phase 2 收集數據後再調整。

### 步驟 1.3：部署 threshold-exporter

在試點環境中部署 threshold-exporter，掛載 conf.d/ 目錄：

```bash
helm repo add vencil https://ghcr.io/vencil/charts
helm repo update

helm install threshold-exporter-redis vencil/threshold-exporter \
  --namespace monitoring \
  --set image.tag=v2.6.0 \
  --set config.dir=/etc/threshold-exporter/conf.d \
  --set replicaCount=2 \
  --values - << 'EOF'
extraVolumes:
  - name: config
    configMap:
      name: threshold-exporter-config-redis
extraVolumeMounts:
  - name: config
    mountPath: /etc/threshold-exporter/conf.d
EOF

kubectl create configmap threshold-exporter-config-redis \
  --from-file=conf.d/redis-prod.yaml \
  -n monitoring \
  --dry-run=client -o yaml | kubectl apply -f -
```

### 步驟 1.4：驗證 Metrics 發出

查詢 threshold-exporter 發出的 metrics：

```bash
kubectl port-forward -n monitoring svc/threshold-exporter-redis 8080:8080 &
curl http://localhost:8080/metrics | grep redis_user_threshold
```

**預期**：出現 `redis_user_threshold_memory_warning`, `redis_user_threshold_memory_critical` 等指標，帶有租戶標籤。

### 步驟 1.5：掛載 Rule Pack

創建包含 Rule Pack 的 ConfigMap，掛載至 Prometheus：

```bash
curl -o rule-pack-redis.yaml \
  https://raw.githubusercontent.com/vencil/vibe-k8s-lab/main/rule-packs/rule-pack-redis.yaml

kubectl create configmap rule-pack-redis \
  --from-file=rule-pack-redis.yaml \
  -n monitoring \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl patch cm prometheus-config -n monitoring --type merge -p '{"data": {"prometheus.yaml": "... (with rule-pack-redis.yaml in rule_files) ..."}}'

kubectl rollout restart deployment/prometheus -n monitoring
```

### 步驟 1.6：驗證 Recording Rules

等待 Prometheus 完成規則加載，驗證 recording rules 產生的指標：

```bash
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &
curl 'http://localhost:9090/api/v1/query?query=redis:memory:usage_percent'
```

**預期**：返回 `redis:memory:usage_percent` 的時序值。

### 步驟 1.7：驗證告警未被路由

確認新的告警已被 Prometheus 產生，但尚未被 Alertmanager 路由：

```bash
curl 'http://localhost:9090/api/v1/alerts' | jq '.data.alerts[] | select(.labels.tenant=="redis-prod")'
curl 'http://localhost:9093/api/v1/alerts' | jq '.[].alerts[] | select(.labels.tenant=="redis-prod")'
```

**預期**：Prometheus 中有新告警，但 Alertmanager 中無對應分組（尚未添加路由）。

### 階段 1 驗證清單

- [ ] threshold-exporter 部署成功，2 個 Pod 運行中
- [ ] metrics 查詢可得到 `redis_user_threshold_*` 系列指標
- [ ] Rule Pack 已掛載，Prometheus 日誌無錯誤
- [ ] Recording Rules 產生輸出
- [ ] Alerting Rules 產生（在 Prometheus 中可見），但未被路由至 Alertmanager receiver

### 階段 1 回滾

若需回滾，執行：

```bash
helm uninstall threshold-exporter-redis -n monitoring
kubectl delete cm rule-pack-redis -n monitoring
kubectl patch cm prometheus-config -n monitoring --type merge -p '{"data": {"prometheus.yaml": "... (original) ..."}}'
kubectl rollout restart deployment/prometheus -n monitoring
```

---

## 階段 2：雙軌並行驗證（雙軌並行驗證）

**目標**：新舊告警同時運作，比較品質。使用 1-2 週時間收集數據，驗證 Dynamic Alerting 的告警品質不低於現有系統。

### Step 2.1: Generate Alertmanager Routing Fragment

使用 `generate-routes` 命令為試點租戶生成 Alertmanager 路由配置：

```bash
da-tools generate-routes \
  --config-dir conf.d/ \
  --tenant redis-prod \
  --output alertmanager-fragment.yaml
```

**預期輸出**：YAML 片段包含新路由（指向 da-pilot-slack receiver，匹配 `tenant=redis-prod`）、優先級設置、group_wait / group_interval / repeat_interval 配置。

### 步驟 2.2：準備雙軌配置

備份現有 Alertmanager 配置，然後在頂部插入新路由：

```bash
cp alertmanager.yaml alertmanager.yaml.backup-phase1

# 使用 kubectl patch 合併配置（避免 cat <<EOF）
kubectl create configmap alertmanager-config-phase2 \
  --from-file=alertmanager.yaml \
  -n monitoring \
  --dry-run=client -o yaml | kubectl apply -f -
```

新路由應於頂部優先匹配 `da_managed: "true" && tenant: redis-prod` 標籤，設 `continue: true` 以允許雙軌記錄。

### 步驟 2.3：預檢查（Shadow Verify Preflight）

運行預檢查，確保雙軌配置合理：

```bash
da-tools shadow-verify preflight \
  --config-dir conf.d/ \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093
```

**預期輸出**：Alertmanager 語法檢查通過、route 優先級無衝突、映射覆蓋率高（>90%）、警告級別合理。若有警告（如 repeat_interval 不一致），評估後決定是否調整。

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

**預期輸出**：JSON 包含告警延遲百分位數、誤報率、分組效果評分、以及與舊告警的對比。

### 步驟 2.5：匯總與決策

基於雙軌期間收集的數據，做出切換決策：

**決策準則**：
- 新告警延遲 < 舊告警延遲（通常 75% 以上改進）
- 新告警誤報率 <= 舊告警誤報率
- 新告警分組 > 舊告警分組（更好的可觀測性）

若三個條件均滿足，進行階段 3 切換。若有疑慮，延長雙軌時間或回滾。

### 階段 2 回滾

若雙軌驗證失敗，恢復至階段 1 結束狀態：

```bash
kubectl patch cm alertmanager-config -n monitoring \
  --type merge -p '{"data": {"alertmanager.yaml": "... (original) ..."}}'
kubectl rollout restart deployment/alertmanager -n monitoring
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

**預期輸出**：Dry-run 報告包含當前狀態（Recording Rules、Alerting Rules、Alertmanager 路由）、計畫操作（禁用舊規則、更新路由優先級）、預期結果、健康檢查、以及回滾命令。

**驗證乾跑輸出**：確認只有舊 Alerting Rules 被禁用，Recording Rules 保持啟用；確認 Alertmanager 路由最終指向新 receiver 且不會重複發送。

### 步驟 3.2：執行切換

確認乾跑結果無誤，執行實際切換：

```bash
da-tools cutover \
  --tenant redis-prod \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093 \
  --execute
```

**執行步驟**：工具自動禁用舊 Alerting Rules（保留 Recording Rules），更新 Alertmanager 路由（移除 `continue: true`，設新 receiver 為唯一路由），移除影子標籤。

### 步驟 3.3：全面健康檢查

切換完成後，執行全面檢查：

```bash
da-tools diagnose \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093 \
  --tenant redis-prod \
  --json \
  > diagnose-post-cutover.json
```

**預期輸出**：診斷報告包含 recording rules 狀態（ACTIVE）、新 alerting rules 狀態（ACTIVE）、舊 alerting rules 狀態（DISABLED）、路由健康度（100%）、cardinality（< 500）。

### 步驟 3.4：確認舊告警已禁用

確認 Alertmanager 中舊告警已消失，Slack channel 中的舊告警流停止：

```bash
curl 'http://localhost:9093/api/v1/alerts' | jq '.[].alerts[] | select(.labels.alertname=="RedisHighMemory" and .labels.da_managed!="true")'
```

**預期**：無返回結果（舊告警已禁用）。

### 階段 3 驗證清單

- [ ] Dry-run 報告確認無異常
- [ ] 切換執行成功，無錯誤日誌
- [ ] Diagnostics 報告：Recording Rules ACTIVE、新 Rules ACTIVE、舊 Rules DISABLED
- [ ] Alertmanager 中舊告警消失，新告警正常發送
- [ ] 相應 Slack channel 中告警流穩定（無重複、無遺漏）

### 階段 3 回滾

若切換失敗，執行回滾：

```bash
da-tools cutover --tenant redis-prod --rollback
```

工具自動重新啟用舊 Alerting Rules，恢復舊 Alertmanager 路由，恢復影子標籤。

---

## 階段 4：擴展與清理（擴展與清理）

**目標**：基於試點成功經驗，批量遷移其他域；完成遺留配置清理；交接文件。

### 步驟 4.1：遷移下一個域（循環）

重複階段 1-3 以遷移下一個域（如 MariaDB）：

```bash
da-tools scaffold \
  --tenant mariadb-prod \
  --db mariadb \
  --non-interactive \
  --output conf.d/mariadb-prod.yaml

# 編輯閾值
# 部署 threshold-exporter（第二個實例）
# 掛載 Rule Pack
# 生成路由
# 雙軌驗證 1-2 週
# 執行切換
```

每個域都獨立經過完整 Phase 1-3，無需互相等待。

### 步驟 4.2：全量驗證

所有域遷移完成後，對全體配置執行驗證：

```bash
da-tools validate-config \
  --config-dir conf.d/ \
  --ci \
  > validation-report.json

# 預期：所有租戶 status = PASS，cardinality violations = 0
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
```

### 步驟 4.4：清理遺留配置

移除不再需要的舊 Prometheus 規則：

```bash
cp prometheus-rules.yaml prometheus-rules.yaml.backup-phase4

# 移除已遷移域的舊規則
grep -v -e "redis" -e "mariadb" -e "kafka" prometheus-rules.yaml \
  > prometheus-rules-cleaned.yaml

diff prometheus-rules.yaml prometheus-rules-cleaned.yaml

kubectl create configmap prometheus-rules-cleaned \
  --from-file=prometheus-rules-cleaned.yaml \
  -n monitoring \
  --dry-run=client -o yaml | kubectl apply -f -
```

### 步驟 4.5：清理測試租戶

如有測試或試驗租戶，移除：

```bash
da-tools ls --config-dir conf.d/
da-tools offboard --tenant test-domain-1
da-tools validate-config --config-dir conf.d/ --ci
```

### 步驟 4.6：更新文件與交接

更新內部文件，記錄遷移完成的各項細節：

```bash
cat > migration-report.yaml << 'EOF'
migration_summary:
  start_date: 2026-03-18
  completion_date: 2026-05-20
  duration_weeks: 9

domains_migrated:
  - name: redis-prod
    phase_3_date: 2026-04-01
    quality_improvement: "75% latency reduction, 100% false positive elimination"
  - name: mariadb-prod
    phase_3_date: 2026-04-23
    quality_improvement: "60% latency reduction"

legacy_rules_removed: 127
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

**A**：不需要。Dynamic Alerting 的 Recording Rules 在現有 scrape 配置之上創建一層乾淨的抽象。即使 scrape 配置混亂，Recording Rules 也能聚合、規範化，產生標準化的指標。遷移完成後可逐步改進 scrape 配置。

### Q2：遷移中途某個域失敗了怎麼辦？

**A**：每個域都是獨立的。若 Redis 切換失敗，只需 `da-tools cutover --tenant redis-prod --rollback`，其他域不受影響。回滾後可重新評估問題，修復後再次嘗試。

### Q3：整個遷移需要多長時間？

**A**：Phase 0（審計）1 天；每個域的 Phase 1-3 需 2-3 週（其中 Phase 2 通常 1-2 週）；Phase 4 清理 2-3 天。典型 5-域遷移耗時 2-3 個月。

### Q4：如何監控 threshold-exporter 的效能？

**A**：`threshold-exporter` 本身暴露 Prometheus metrics。查詢 `threshold_exporter_scrape_duration_seconds` 確認掃描延遲，查詢 `threshold_exporter_metrics_generated` 確認産出指標數。

### Q5：Double 告警（舊新都發）怎麼辦？

**A**：Phase 2 設 `continue: true` 允許新舊告警同時路由，這是設計的一部分。切換時（Phase 3）禁用舊規則即可消除重複。

### Q6：Rule Pack 不適用怎麼辦？

**A**：若某域的指標不符合 Rule Pack 預期，保留在舊配置中。Dynamic Alerting 支援漸進式遷移——部分域使用 Rule Pack，部分域仍用舊規則。

---

## 遷移時間線（典型 5 域案例）

| 階段 | 時間 |
|------|------|
| Phase 0（全局審計） | 1 天 |
| Phase 1-3（Redis） | 3 週 |
| Phase 1-3（MariaDB） | 2 週 |
| Phase 1-3（Kafka） | 2 週 |
| Phase 1-3（JVM） | 1.5 週 |
| Phase 1-3（自定義） | 2.5 週 |
| Phase 4（清理） | 2 天 |
| **總計** | **～11 週（2.5 個月）** |

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
