---
title: "場景：漸進式遷移 Playbook"
tags: [scenario, migration, adoption, playbook]
audience: [platform-engineer, sre]
version: v2.9.0
lang: zh
---

# 場景：漸進式遷移 Playbook

> **Language / 語言：** **中文 (Current)** | [English](./incremental-migration-playbook.en.md)

> **v2.9.0** | 相關文件：[`migration-guide.md`](../migration-guide.md)、[`shadow-monitoring-cutover.md`](shadow-monitoring-cutover.md)、[`architecture-and-design.md` §2](../architecture-and-design.md)

## 概述

本 Playbook 指引企業從現有的混亂 Prometheus + Alertmanager 部署漸進式遷移至 Dynamic Alerting 平台，**零停機時間**。核心原則是 **Strangler Fig Pattern**：在既有系統上方建構一層乾淨的覆蓋層，逐步取代舊架構，不必先清理底層。

每個階段都是**獨立有價值的**——企業可以在任何階段停止，無需擔心系統癱瘓。遷移的速度完全由你掌控。

## 前置條件

- 運行中的 Prometheus 實例（`http://prometheus:9090`）
- 運行中的 Alertmanager（`http://alertmanager:9093`）
- Kubernetes 叢集（Kind、EKS、GKE 均可）
- `da-tools` 映像已推送至私有 registry 或可公開存取（`ghcr.io/vencil/da-tools:v3.0.0`）
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
  --output-dir onboard-audit
```

**預期輸出**：stderr 列出每個 receiver 是否帶租戶 matcher（`Found N tenant route(s) (of M total)`，沒有的逐一 `SKIP`；要存檔記得導 `2>`，只導 stdout 會拿到只有檔案清單的輸出），並寫出目錄 `onboard-audit/`（`-o/--output-dir` 吃的是**目錄**，給檔名會得到一個同名目錄）——裡面是 `phase1-routing/routing-summary.csv`：每個租戶 route 的 receiver 類型、`group_wait`／`group_interval`／`repeat_interval` 與 severity dedup 判定；找到租戶 route 時另有每個租戶一份 `phase1-routing/<租戶>.yaml` 路由片段，以及 `onboard-hints.json`。分析要點：
- Receiver 數量 → 潛在租戶數量
- 現有 group_wait / repeat_interval → 後續 Dynamic Alerting 的 Routing Guardrails 參考值
- Inhibit rules → 是否需要遷移至 Dynamic Alerting 的 severity dedup 機制

### Step 0.2: Analyze Existing Prometheus Alert Rules

分析現有規則，按類型分類（Recording Rules / Alerting Rules），識別遷移候選：

```bash
da-tools onboard \
  --rule-files '/etc/prometheus/rules.d/*.yaml' \
  --output-dir rule-audit
```

`--rule-files` 只收一個 glob（支援 `**`），規則檔散在多處時先集中到同一個目錄；⚠️ glob 要加引號，否則 shell 先展開成多個檔名，rc=2。

**預期輸出**：stderr 印出掃描摘要（`Scanned N file(s), M rule(s) in K group(s)`、Alert rules 其中可解析／不可解析各幾條、Recording rules 幾條），並寫出目錄 `rule-audit/phase2-rules/`：
- `migration-plan.csv`：每條告警規則一列，含 metric、閾值、運算子、建議的聚合方式，以及 `status`（`perfect` 可直接轉換、`complex` 需人工確認、`unparseable` 無法解析）
- `_defaults-suggestion.yaml`：由既有閾值推得的平台預設值建議。⚠️ `complex` 規則的閾值也在裡面，合併前要和 `migration-plan.csv` 逐條對過；規則全部 `unparseable` 時不會產生這個檔

依 `status` 排遷移順序：`perfect` 先遷，`complex` 逐條人工確認，`unparseable` 留到最後或保留原規則。

### 步驟 0.3：掃描叢集中的現有告警活動

掃描 Prometheus 中所有活躍的 scrape targets，了解實際監控的內容：

```bash
da-tools blind-spot \
  --config-dir /dev/null \
  --prometheus http://prometheus:9090 \
  --json-output \
  > blind-spot-report.json
```

**預期輸出**：`blind-spot-report.json` 是一個陣列，每個元素對應一種由 scrape job 名稱推得的 DB 類型：`live_instances`（叢集裡的實例）、`monitored_tenants`（已有哪些租戶在監控），以及 `status`。此時還沒有任何租戶配置（`--config-dir /dev/null`，stderr 會印一行 `WARN: config-dir not found` 屬預期），所以每種辨識得出的 DB 類型都是 `blind_spot`；job 名稱對不上任何 DB 類型的實例歸在 `unrecognized`。這份清單就是步驟 0.4 挑試點域時的候選範圍；決策矩陣的 `rule_pack_coverage` 就拿這裡的 DB 類型對照 [Rule Packs README](../rule-packs/README.md) 評。⚠️ Prometheus 連不到時輸出是 `[]`、結束碼仍是 0（stderr 會有 `WARN: Cannot reach Prometheus`），不要讀成「叢集裡沒有東西」。

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

**目標**：為選定的單一域（如 Redis）在 Dynamic Alerting 平台部署，與現有告警並行。Rule Pack 的告警開始評估；加上租戶路由之前，它們會落到舊 Alertmanager 設定裡符合的 route（見步驟 1.8）。

### 步驟 1.1：生成租戶配置

基於 Phase 0 的決策，使用 `scaffold` 命令生成初始配置：

```bash
mkdir -p conf.d/

da-tools scaffold \
  --tenant redis-prod \
  --db redis \
  --non-interactive \
  --output-dir conf.d
```

**預期輸出**：`conf.d/redis-prod.yaml` 包含 recording rules 設定、threshold 初始值（conservative）、路由配置（初始禁用）；同一個目錄下另有 `_defaults.yaml`（平台預設值）與 `scaffold-report.txt`。`-o/--output-dir` 吃的是**目錄**——寫 `--output conf.d/redis-prod.yaml` 會得到一個叫 `redis-prod.yaml` 的目錄、租戶檔在它裡面多一層。

### 步驟 1.2：編輯閾值配置

基於 Phase 0.2 的審計輸出，調整 threshold 參數以符合現有規則的邏輯。**重點是保守設置**，寧願在 Phase 2 收集數據後再調整。

### 步驟 1.3：部署 threshold-exporter

chart 以 OCI 發布（[ADR-002](../adr/002-oci-registry-over-chartmuseum.md)）。租戶設定放在 values 的 `thresholdConfig.tenants`，chart 會把它渲染成 `threshold-config` ConfigMap，一個 exporter 服務所有租戶。

⚠️ chart 出貨的 `thresholdConfig.defaults` 只含 Kubernetes、MariaDB、PostgreSQL 的預設值，redis 的 key 要自己補。從步驟 1.1 scaffold 產出的 `conf.d/_defaults.yaml` 抄 `redis_*` 那幾行即可。沒補的話，租戶設了也不會發射，exporter 會記一行 `unknown key "redis_memory_used_bytes" not in defaults`。

```yaml
# values-redis.yaml
thresholdConfig:
  defaults:                 # chart 出貨不含 redis；取自 scaffold 產出的 conf.d/_defaults.yaml
    redis_memory_used_bytes: 4294967296
    redis_connected_clients: 200
    redis_evicted_keys_rate: 100
    redis_replication_lag: 30
  tenants:
    redis-prod:             # 內容＝conf.d/redis-prod.yaml 裡 tenants.redis-prod 底下那一層
      redis_memory_used_bytes: "8589934592"
```

```bash
helm upgrade --install threshold-exporter \
  oci://ghcr.io/vencil/charts/threshold-exporter --version 2.9.0 \
  -n monitoring -f values-redis.yaml
```

### 步驟 1.4：讓資料庫指標帶上 `tenant` 標籤

Rule Pack 以 `on(tenant)` 把指標和閾值配對，所以 Redis exporter 的 scrape job 要用 relabel 注入 `tenant` 標籤，做法見 [BYO Prometheus 整合指南](../integration/byo-prometheus-integration.md)（方案 A：以 namespace 當 tenant）。沒有這一步，Rule Pack 的告警不會觸發。

⚠️ relabel 之後，直接寫在這些指標上的**舊規則**，產生的告警也會帶 `tenant="redis-prod"`。這會影響階段 2 的路由，見步驟 2.2。

### 步驟 1.5：驗證 Metrics 發出

```bash
kubectl port-forward -n monitoring svc/threshold-exporter 8080:8080 &
curl -s http://localhost:8080/metrics | grep 'user_threshold{component="redis"'
```

**預期**（每個 redis key 一列；`component` 是 key 第一個 `_` 之前的部分，`metric` 是之後的部分）：

```
user_threshold{component="redis",metric="connected_clients",severity="warning",tenant="redis-prod"} 200
user_threshold{component="redis",metric="evicted_keys_rate",severity="warning",tenant="redis-prod"} 100
user_threshold{component="redis",metric="memory_used_bytes",severity="warning",tenant="redis-prod"} 8.589934592e+09
user_threshold{component="redis",metric="replication_lag",severity="warning",tenant="redis-prod"} 30
```

### 步驟 1.6：掛載 Rule Pack

Rule Pack 的 ConfigMap 由 repo 產生好了，直接套用：

```bash
kubectl apply -f k8s/03-monitoring/configmap-rules-redis.yaml
```

平台提供的 Prometheus Deployment 已經用 Projected Volume 把 `prometheus-rules-redis` 掛到 `/etc/prometheus/rules/`（`optional: true`），套用後會自動載入。自備 Prometheus 時，掛載方式見 [BYO Prometheus 整合指南](../integration/byo-prometheus-integration.md)。

### 步驟 1.7：驗證 Recording Rules

```bash
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &
curl -s 'http://localhost:9090/api/v1/query?query=tenant:redis_memory_usage:ratio'
curl -s 'http://localhost:9090/api/v1/query?query=tenant:alert_threshold:redis_memory_used_bytes'
```

**預期**：兩個查詢都回傳帶 `tenant="redis-prod"` 的時序。第一個是資料面（需要步驟 1.4 的 relabel），第二個是閾值面（需要步驟 1.3 補的 defaults）。

### 步驟 1.8：確認新告警目前會被送到哪裡

加上租戶路由之前，新告警會落到舊 Alertmanager 設定裡第一條符合的 route，通常就是 root receiver。用 `amtool` 對現有設定測一次：

```bash
amtool config routes test --config.file=alertmanager.yaml \
  alertname=RedisHighMemory tenant=redis-prod severity=warning
```

以一份只有 root receiver `legacy-default`、外加一條 `severity="critical"` → `legacy-pager` 子路由的舊設定為例，輸出是 `legacy-default`：Rule Pack 的告警已經會出現在舊頻道。舊告警和新告警同樣帶 `tenant`，所以沒辦法只靠 `tenant` 把新告警擋在舊頻道之外；要擋，只能另外寫一條以 Rule Pack 的 alertname 比對的路由，而且前提是舊規則沒有用到同樣的名稱。

### 階段 1 驗證清單

- [ ] threshold-exporter 部署成功，2 個 Pod 運行中
- [ ] `/metrics` 查得到 `user_threshold{component="redis",…}` 各列
- [ ] Redis 指標帶上 `tenant` 標籤
- [ ] Rule Pack 已掛載，Prometheus 日誌無錯誤
- [ ] 兩個 recording rule 查詢都有輸出
- [ ] 已用 `amtool config routes test` 確認新告警目前的去向

### 階段 1 回滾

```bash
helm uninstall threshold-exporter -n monitoring
kubectl delete -f k8s/03-monitoring/configmap-rules-redis.yaml
# 再拿掉步驟 1.4 加的 relabel 設定，reload Prometheus
```

---

## 階段 2：雙軌並行驗證（雙軌並行驗證）

**目標**：新舊告警同時運作，比較品質。使用 1-2 週時間收集數據，驗證 Dynamic Alerting 的告警品質不低於現有系統。

### Step 2.1: Generate Alertmanager Routing Fragment

先在租戶檔加上 `_routing`（receiver 的完整 schema 見 [BYO Alertmanager 整合指南](../integration/byo-alertmanager-integration.md)）：

```yaml
# conf.d/redis-prod.yaml
tenants:
  redis-prod:
    redis_memory_used_bytes: "8589934592"
    _routing:
      receiver:
        type: slack
        api_url: "https://hooks.slack.com/services/T000/B000/XXXX"
        channel: "#da-pilot"
      group_wait: 30s
      group_interval: 5m
      repeat_interval: 4h
```

再產生路由片段：

```bash
da-tools generate-routes \
  --config-dir conf.d/ \
  -o alertmanager-fragment.yaml
```

**預期輸出**：stderr 印 `Found 1 tenant(s) with routing config: redis-prod` 與 `Written to alertmanager-fragment.yaml (1 routes, 1 receivers, 1 inhibit rules)`。片段裡的 route 以 `tenant="redis-prod"` 比對、送往 `tenant-redis-prod` receiver；另有一條同租戶 critical 抑制 warning 的 inhibit rule。工具一次產生 conf.d 裡**所有**有 `_routing` 的租戶，沒有只產單一租戶的選項（尚未實作）；沒有 `_routing` 的租戶不會產生 route。

### 步驟 2.2：準備雙軌配置

⚠️ 不要用 `generate-routes --apply` 或 `--output-configmap --base-config` 合併進舊的 Alertmanager 設定：這兩個模式會用產生的 route **取代**整個 `route.routes`，舊的子路由會全部消失。雙軌期間請手動合併：

```bash
cp alertmanager.yaml alertmanager.yaml.backup-phase1
```

把片段的 route 插到 `route.routes` 的**最前面**並手動加上 `continue: true`（工具不會產生它），片段的 receiver 與 inhibit rule 也加進去，最後在 `route.routes` 末尾補一條沒有 matcher 的 catch-all，指回原本的 root receiver：

```yaml
# alertmanager.yaml（雙軌期間）
route:
  receiver: legacy-default
  group_by: [alertname]
  routes:
  - matchers: ['tenant="redis-prod"']    # generate-routes 產生的 route
    receiver: tenant-redis-prod
    group_wait: 30s
    group_interval: 5m
    repeat_interval: 4h
    continue: true                       # 手動加
  - matchers: ['severity="critical"']    # 舊設定原有的子路由，原封不動
    receiver: legacy-pager
  - receiver: legacy-default             # 手動加：catch-all
receivers:
- name: legacy-default
  webhook_configs: [{url: 'http://legacy.example/default'}]
- name: legacy-pager
  webhook_configs: [{url: 'http://legacy.example/pager'}]
- name: tenant-redis-prod
  slack_configs:
  - api_url: https://hooks.slack.com/services/T000/B000/XXXX
    channel: '#da-pilot'
inhibit_rules:
- source_matchers: ['severity="critical"', 'metric_group=~".+"', 'tenant="redis-prod"']
  target_matchers: ['severity="warning"', 'metric_group=~".+"', 'tenant="redis-prod"']
  equal: [metric_group]
```

為什麼兩樣都要手動加：

- 少了 `continue: true`，所有帶 `tenant="redis-prod"` 的告警都只會進新頻道。步驟 1.4 之後舊告警也帶 `tenant`，所以舊的 critical 告警會從 `legacy-pager` 消失。
- 只加 `continue`、不加 catch-all，舊的 warning 告警還是會離開 `legacy-default`：只要有任何子路由比對成功，Alertmanager 就不會退回 root receiver。

合併後先驗語法，再逐類測去向：

```bash
amtool check-config alertmanager.yaml
amtool config routes test --config.file=alertmanager.yaml \
  alertname=RedisHighMemory tenant=redis-prod severity=warning
amtool config routes test --config.file=alertmanager.yaml \
  alertname=RedisDownLegacy tenant=redis-prod severity=critical
```

**預期**：第一個測試輸出 `tenant-redis-prod,legacy-default`，第二個輸出 `tenant-redis-prod,legacy-pager`。雙軌期間兩個頻道都會收到該租戶的新舊告警，要靠 alertname 區分哪些是 Rule Pack 的（清單見 [Rule Pack 告警參考](../rule-packs/ALERT-REFERENCE.md)）。

### 步驟 2.3：預檢查

```bash
da-tools generate-routes --config-dir conf.d/ --validate
amtool check-config alertmanager.yaml
```

**預期**：`--validate` 檢查租戶的 `_routing`（receiver 格式、時序參數護欄、routing profile 引用與 domain policy），沒有錯誤時結束碼 0；`amtool check-config` 驗合併後的整份設定。`da-tools shadow-verify preflight` 是給 migrate／shadow 流程用的（檢查 prefix mapping、`migration_status: shadow` 的規則與 Alertmanager 的 shadow 攔截），這條流程用不到。

### 步驟 2.4：監控雙軌運行（1-2 週）

讓系統並行運作 1-2 週，期間實時觀察兩個 Slack channels 中的告警：

```bash
# 每天運行一次質量評估
da-tools alert-quality \
  --prometheus http://prometheus:9090 \
  --tenant redis-prod \
  --period 24h \
  --json \
  > alert-quality-$(date +%Y-%m-%d).json
```

**預期輸出**：JSON 的 `tenants[]` 以租戶為單位，每個 alertname 一筆：噪音（單位時間觸發次數）、陳舊度（距上次觸發幾天）、平均解除時間、被 inhibit／silence 壓制的比例，以及 good／warn／bad 等級，另有租戶總分與 `summary`。工具不會拿新告警和舊告警直接對比，也沒有誤報率。

### 步驟 2.5：匯總與決策

基於雙軌期間收集的數據，做出切換決策：

**決策準則**：
- Rule Pack 的告警在 `alert-quality` 裡沒有 `bad` 等級，或每一個 `bad` 都已找到原因並調整閾值
- 舊頻道裡該域真正需要處理的每一次告警，新頻道都有對應的 Rule Pack 告警（逐次對照兩個頻道）
- 值班人員確認新告警的內容（summary、runbook）足以取代舊告警

若三個條件均滿足，進行階段 3 切換。若有疑慮，延長雙軌時間或回滾。

### 階段 2 回滾

若雙軌驗證失敗，恢復至階段 1 結束狀態：用步驟 2.2 的備份覆蓋 Alertmanager 設定（依你的部署方式更新 ConfigMap 或設定檔），再 reload：

```bash
curl -X POST http://localhost:9093/-/reload
```

---

## 階段 3：切換（切換）

**目標**：移除試點域的舊告警規則，並讓試點租戶的告警只送到新 receiver，使 Dynamic Alerting 成為主告警來源。系統無中斷。

`da-tools cutover` **不適用**這條流程：它是 migrate／shadow 流程的切換工具，需要 `validate_migration` 產生的 readiness JSON，動作是刪除 `shadow-monitor` Job 與 `prometheus-rules-old` ConfigMap、移除 `migration_status` label，這些物件在這條流程裡都不存在。改用下面的手動步驟；migrate／shadow 流程見 [Shadow Monitoring 切換](shadow-monitoring-cutover.md)。

### 步驟 3.1：乾跑切換預演

在實際執行前，對兩份設定各做一次離線檢查：

```bash
cp prometheus-rules.yaml prometheus-rules.yaml.backup-phase3
cp alertmanager.yaml alertmanager.yaml.backup-phase3

# 1. 舊規則：依步驟 0.2 的 migration-plan.csv，在 prometheus-rules.yaml 裡整條刪除
#    試點域的舊告警規則（連同 expr、labels 整段），再驗格式
promtool check rules prometheus-rules.yaml

# 2. Alertmanager：拿掉試點租戶 route 的 continue: true，再測去向
amtool config routes test --config.file=alertmanager.yaml \
  alertname=RedisHighMemory tenant=redis-prod severity=warning
```

**預期**：`promtool` 回報 `SUCCESS`；`amtool` 只輸出 `tenant-redis-prod`。

**驗證乾跑輸出**：確認被刪的只有試點域的舊告警規則，其他域的規則仍在；確認其他（沒有 `tenant="redis-prod"` 的）告警的去向沒變。

### 步驟 3.2：執行切換

確認乾跑結果無誤，執行實際切換：把兩份設定套用到叢集（依你的部署方式更新 ConfigMap 或設定檔），再重載：

```bash
kubectl rollout restart deployment/prometheus -n monitoring
curl -X POST http://localhost:9093/-/reload
```

### 步驟 3.3：全面健康檢查

切換完成後，確認 Rule Pack 的告警照常評估：

```bash
da-tools check-alert RedisHighMemory redis-prod \
  --prometheus http://prometheus:9090
```

**預期輸出**：該告警對 `redis-prod` 目前的狀態（firing／pending／inactive）。⚠️ `da-tools diagnose` 的健康檢查固定查租戶 namespace 裡 `app=mariadb` 的 Pod，對 Redis 租戶會回 `Pod not found`、`status: error`，不適合用在這一步。

### 步驟 3.4：確認舊告警已停止送達

用 Alertmanager v2 API 列出試點租戶目前的告警與它們被送往的 receiver：

```bash
curl -sG http://localhost:9093/api/v2/alerts \
  --data-urlencode 'filter=tenant="redis-prod"' \
  | jq -r '.[] | "\(.labels.alertname) → \([.receivers[].name] | join(","))"'
```

**預期**：只出現 Rule Pack 的 alertname，而且都只送往 `tenant-redis-prod`。⚠️ Alertmanager v0.27.0 起移除了 v1 API，`/api/v1/alerts` 會回 HTTP 410。

### 階段 3 驗證清單

- [ ] `promtool check rules` 與 `amtool check-config` 都通過
- [ ] 切換後 Prometheus 與 Alertmanager 日誌無錯誤
- [ ] `check-alert` 查得到 Rule Pack 告警的狀態
- [ ] Alertmanager 裡試點租戶只剩 Rule Pack 的告警，且只送往新 receiver
- [ ] 相應 Slack channel 中告警流穩定（無重複、無遺漏）

### 階段 3 回滾

若切換失敗，用步驟 3.1 的兩份備份（`*.backup-phase3`）還原並重載：舊規則恢復觸發，試點租戶的 route 恢復 `continue: true`，回到雙軌狀態。

```bash
kubectl rollout restart deployment/prometheus -n monitoring
curl -X POST http://localhost:9093/-/reload
```

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
  --output-dir scaffold_output
# 只搬租戶檔：scaffold 每次都會重新產生 _defaults.yaml，
# 直接指到 conf.d 會把步驟 1.2 調過的平台預設值蓋掉
cp scaffold_output/mariadb-prod.yaml conf.d/

# 編輯閾值
# 把新租戶（與 chart 出貨沒有的 defaults）加進同一份 values，
#   helm upgrade 同一個 release（不需要第二個 exporter）
# relabel、掛載 Rule Pack
# 生成路由、雙軌驗證 1-2 週
# 執行切換
```

每個域都獨立經過完整 Phase 1-3，無需互相等待。

### 步驟 4.2：全量驗證

所有域遷移完成後，對全體配置執行驗證：

```bash
da-tools validate-config \
  --config-dir conf.d/ \
  --json \
  > validation-report.json

# 預期：結束碼 0（任一檢查項 fail 時結束碼為 1，不需要另加旗標）。
# validation-report.json 是一份檢查項清單，每一項形如
#   {"check": "schema", "status": "pass", "details": [...]}
# status 為 pass / warn / fail；沒有任何一項是 fail 才算通過。
```

### 步驟 4.3：批量診斷

對所有租戶執行健康檢查（租戶清單自動取自 chart 建立的 `threshold-config` ConfigMap）：

```bash
da-tools batch-diagnose \
  --prometheus http://prometheus:9090 \
  --json \
  > batch-diagnose.json
```

**預期**：每個租戶一筆，`status` 為 `healthy` 或 `error`。⚠️ 逐租戶的檢查就是 `diagnose`，固定查 `app=mariadb` 的 Pod，所以非 MariaDB 的租戶會是 `error`（`Pod not found`）；這些租戶改用 `check-alert` 逐一確認。批量診斷不讀 conf.d，也不查 Alertmanager。

### 步驟 4.4：清理遺留配置

各域的舊告警規則已在各自的階段 3 刪除。全部域遷移完成後，確認舊規則檔只剩刻意保留的規則（例如 Rule Pack 不適用的域），驗證格式後再套用：

```bash
cp prometheus-rules.yaml prometheus-rules.yaml.backup-phase4
promtool check rules prometheus-rules.yaml
```

⚠️ 不要用 `grep -v` 逐行刪規則：它只會刪掉含關鍵字的那幾行，留下缺了 `expr` 的殘缺規則（`promtool` 報 `field 'expr' must be set in rule`），而且名稱不含關鍵字的規則（例如 MariaDB 域的 `MySQL…`）根本刪不到。

### 步驟 4.5：清理測試租戶

如有測試或試驗租戶，移除：

```bash
find conf.d -name 'tenant-*.yaml'
da-tools offboard test-domain-1 --config-dir conf.d/            # Pre-check
da-tools offboard test-domain-1 --config-dir conf.d/ --execute  # 實際下架
da-tools validate-config --config-dir conf.d/
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

## Emergency Rollback Procedures（緊急退版程序）

> **適用情境**：客戶 cutover 後發現結構性 bug（例：parser 吃掉 label / Dangling Guard 誤判 / 大量 false positive），需要把 Base Infrastructure PR + 多個 tenant PR 整批退版。
> 與「階段 0/1/2/3 回滾」的差異：那些是試點期單一域的回退；本節是 cutover 後 batch PR pipeline 的整體退版，要處理 cascading defaults 與 hierarchical 依賴。
>
> **此程序為 v2.8.0 新增**（配合 Migration Batch PR Pipeline 的 hierarchy-aware chunking 設計）。

### 退版順序：merge 順序的嚴格反序

**禁止「隨意按 GitHub Revert」**——hierarchical 結構下 cascading defaults 有依賴關係，亂序退版會觸發 cycling reload 與中間態誤觸發告警。正確順序為 merge 時的反序：

| Merge 階段（forward） | 退版階段（reverse） |
|---|---|
| 1. `[Base Infrastructure PR]` —— `_defaults.yaml` 變更 | **退版 step 4**（最後）|
| 2. cascading defaults（outer → inner） | **退版 step 3**（按 inner → outer 逆序）|
| 3. tenant PR chunk 1（`Blocked by: #base-pr`） | **退版 step 2**（與 chunk 4-N 平行）|
| 4. tenant PR chunks 2-N | **退版 step 1**（最先）|

**為什麼 inner 先退**：tenant PR 不退而先退 outer `_defaults.yaml`，會讓 inner tenant 的 `effective_config` 瞬間落到「outer defaults 已退、tenant override 還在」的混合態 —— inheritance graph 計算的 merged_hash 會與 base PR merge 前的歷史值都不相符。

### WatchLoop debounce 驗收（與 v2.7.0 觀測 metrics 連動）

退版 wave 期間 git-sync 會把多個 commit 在 ~50-300ms 內依序 apply，觸發連續 fsnotify 事件。`config_debounce.go` 的 sliding-window debounce 必須把整個 wave 收斂為**單次** reload，中間態禁止誤 fire alert。

**驗證 metrics**（v2.8.0 PR #75 加入）：

```promql
# 退版 wave 期間，預期 sliding window 把所有 reload 觸發合併為單次 fire：
# fire 一次 ⇒ reloadDuration 1 sample；多次 fire ⇒ 多 sample（壞訊號）
increase(da_config_reload_duration_seconds_count[5m])

# debounce_batch sum 應接近 wave 內變更檔案數：sum/count 比即「平均收斂率」
# 1 = 沒收斂（每個事件單獨 fire）；wave 大小 = 完美收斂
sum(rate(da_config_debounce_batch_size_sum[5m]))
  /
sum(rate(da_config_debounce_batch_size_count[5m]))
```

**新增 test case（v2.8.0 follow-up，預定排在 Migration Batch PR Pipeline 實作 PR）**：`TestDebouncedReload_RollbackWave` —— 模擬 N 個 git-sync commit 反序 apply、debounce window 100ms，斷言 fire count 永遠 = 1（與 `TestSlowWriteTornStateStress` 同 pattern，差別在「反向時間序列 + cascading defaults 同步退」）。設計細節留待 batch-PR pipeline 實作 PR。

### Staging rehearsal 強制（cutover 前 2 週）

**未跑過 rehearsal 不准正式 cutover** —— 寫入 cutover checklist 作為 hard gate（Customer Delivery 客戶里程碑日曆）。

Rehearsal 內容：
1. 客戶 staging 環境完整套用一輪 batch PR pipeline 產出（不限規模，最少 1 個 Base PR + 5 個 tenant PR）
2. **立刻** 跑反序退版（不等任何 cooldown）
3. 量測每段時間：每個 PR 的 review/merge/git-sync apply/WatchLoop settle 各約幾秒，整輪退版到 `merged_hash` 收斂回 Base PR merge 前狀態的端到端時間
4. 數據填入下方「退版時間預算表」，正式 cutover 時若實測時間超過預算 1.5 倍即視為異常需 abort

### 退版時間預算表（基於 Phase 1 baseline @ 1000-tenant，PR #59）

> **數據來源**：`config_hierarchy_bench_test.go` PR #59 baseline（1000/2000/5000-tenant scaling characterization）。
> **適用範圍**：tenant 數 ≤ 1000 視為 baseline；2000-5000 套 §「Phase 1 scaling characterization」線性外推（5×=4.6-5.4×）。

| 退版動作 | 1000-tenant 預算 | 5000-tenant 預算 | 來源 |
|---|---|---|---|
| 單個 tenant PR revert + git-sync apply | < 5s | < 5s | git-sync polling 5s + scan_dir ≈ 51-273ms |
| 單個 `_defaults.yaml` revert（region 級）→ 21t affected @ 1000 / 105t @ 5000 | < 600ms reload | < 1.5s reload | BlastRadius bench 266ms / 1308ms |
| 整波退版（Base PR + 10 tenant PR + 2 cascading defaults）| < 90s | < 4 min | git-sync poll × N + reload × N |
| `merged_hash` 收斂驗證 | < 30s | < 2 min | `da-tools tenant-verify --all` |

**門檻**：實測超過上表 1.5 倍視為異常 → **暫停退版**，先讀 `da_config_reload_duration_seconds` p99 與 `da_config_blast_radius_tenants_affected{effect="applied"}` 看是否落在預期分佈，異常找 maintainer 介入。

### 驗證 checklist（退版完成後逐項打勾）

```
[ ] 1. 全部 N 個 PR 都已 revert（git log --oneline | head -N 全為 "Revert ..." commit）
[ ] 2. git-sync 已 apply 全部 revert（kubectl exec git-sync -- git rev-parse HEAD == 預期 SHA）
[ ] 3. da_config_reload_trigger_total{reason="defaults"} 從 wave 開始增量 == 預期 cascading defaults 變更檔數
[ ] 4. da_config_reload_duration_seconds_count 從 wave 開始增量 == 1（debounce 收斂正確）
[ ] 5. da_config_blast_radius_tenants_affected{effect="applied"} 增量 sum ≈ 預期受影響 tenant 數
[ ] 6. 抽樣 5 個 tenant：da-tools tenant-verify <id> --expect-merged-hash <pre-base-snapshot> （exit code 0 = 通過，2 = 不一致／tenant 不存在／重複宣告）
[ ] 7. 過去 10 分鐘 ALERTS{severity!="info"} 總數 ≤ wave 開始前的 baseline + 5%
[ ] 8. Alertmanager Silenced alerts 列表為空（沒有遺留 silence 干擾觀測）
```

**第 6 項是核心**：checksum 必須回到 Base PR merge 前的 `merged_hash`，若不一致即代表 drift（可能某個 tenant PR 被部分退版、或某 cascading defaults 漏退）；先把 pre-Base-PR snapshot 用 `da-tools tenant-verify --all --json > pre-base.json` 存起來，rollback 後再跑同樣指令對照即可定位漂移 tenant。exit 2 且輸出 `status: ERROR — duplicate` 時不是 hash 漂移，而是**重複宣告**：該 tenant 同時出現在 `declared in:` 列出的多個檔裡（例如 rollback 漏刪的檔），工具因此拒絕算 hash——從多餘的檔移除這個 tenant 的宣告、讓它只留在一個檔後重跑第 6 項（同一檔可能還宣告了其他 tenant，只有該檔沒有其他要保留的內容時才整個刪掉）。`--all` 遇到重複宣告同樣 exit 2（其餘 tenant 照常輸出），拍快照前先處理掉。

### 工具層 follow-up（未實裝，列入 v2.8.x backlog）

- `make rollback-dryrun` Makefile target —— 客戶 staging 環境一鍵跑 staging rehearsal，自動填上方時間預算表
- `da-tools batch-pr rollback --plan` —— 從 Base PR # 反推完整退版順序，產出 markdown plan 給 ops review
- `da-tools batch-pr rollback --execute` —— 自動執行反序 revert，每步等 git-sync apply + reload 收斂後才前進

這些工具不在本文件 PR 範圍，但本節定義的程序為其 specification。

---

## 常見問題（FAQ）

### Q1：遷移前需要清理 scrape 配置嗎？

**A**：不需要整理，但要加一件事：資料庫 exporter 的 scrape job 要用 relabel 注入 `tenant` 標籤（步驟 1.4）。除此之外，Dynamic Alerting 的 Recording Rules 在現有 scrape 配置之上創建一層乾淨的抽象，把各種 exporter 的指標聚合、規範化成標準化的指標。遷移完成後可逐步改進 scrape 配置。

### Q2：遷移中途某個域失敗了怎麼辦？

**A**：每個域都是獨立的。若 Redis 切換失敗，照「階段 3 回滾」用 `*.backup-phase3` 還原舊規則與 Alertmanager 設定，其他域不受影響。`da-tools cutover` 沒有回退選項（尚未實作），而且它屬於 migrate／shadow 流程。回滾後可重新評估問題，修復後再次嘗試。

### Q3：整個遷移需要多長時間？

**A**：Phase 0（審計）1 天；每個域的 Phase 1-3 需 2-3 週（其中 Phase 2 通常 1-2 週）；Phase 4 清理 2-3 天。典型 5-域遷移耗時 2-3 個月。

### Q4：如何監控 threshold-exporter 的效能？

**A**：`threshold-exporter` 本身暴露 Prometheus metrics。`da_config_scan_duration_seconds` 看設定掃描耗時，`da_config_reload_duration_seconds` 看重載耗時，`count(user_threshold)` 看目前發射的閾值列數。

### Q5：Double 告警（舊新都發）怎麼辦？

**A**：Phase 2 的試點租戶 route 設 `continue: true`、末尾補 catch-all，新舊兩個頻道都會收到該租戶的新舊告警，這是雙軌的設計（見步驟 2.2）。切換時（Phase 3）刪除舊規則、拿掉 `continue: true` 即可消除重複。

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
