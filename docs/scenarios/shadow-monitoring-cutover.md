---
title: "場景：Shadow Monitoring — 從告警健康評估到全自動切換"
tags: [scenario, shadow-monitoring, cutover, alert-quality]
audience: [platform-engineer, sre, devops, tenant]
version: v2.9.0
lang: zh
---
# 場景：Shadow Monitoring — 從告警健康評估到全自動切換

> **Language / 語言：** **中文 (Current)** | [English](./shadow-monitoring-cutover.en.md)

> **v2.9.0** | 相關文件：[`shadow-monitoring-sop.md`](../shadow-monitoring-sop.md)、[`migration-guide.md`](../migration-guide.md)、[`CLI Reference`](../cli-reference.md)

本指南涵蓋從告警品質評估到完整遷移切換的端對端流程：Phase 0（評估）→ Phase 1–6（遷移 & 切換）。

## Phase 0：告警品質評估（不需部署任何元件）

在決定是否遷移之前，先用 `da-tools alert-quality` 量化現有告警品質。此工具直接連接現有 Prometheus/Alertmanager，不需要部署 Dynamic Alerting 元件。

### 四大品質指標

| 指標 | 衡量 | 判定標準 |
|------|------|---------|
| **Noise Score** | 單位時間 firing 次數 | >20 = BAD, >10 = WARN |
| **Stale Score** | 距離上次 fire 天數 | >14 天 = WARN |
| **Resolution Latency** | firing → resolved 平均時間 | <5 分鐘 = flapping（BAD） |
| **Suppression Ratio** | 被 inhibit/silence 壓制比例 | >50% = WARN |

### 執行品質掃描

```bash
# 掃描全部 tenant，分析過去 30 天
docker run --rm --network host \
  ghcr.io/vencil/da-tools:v3.0.0 alert-quality \
  --prometheus http://localhost:9090 \
  --period 30d

# 單一 tenant + JSON 輸出
da-tools alert-quality --prometheus http://localhost:9090 --period 30d \
  --tenant db-a --json > audit-report.json
```

### 根據結果決策

| 分數區間 | 建議行動 |
|---------|---------|
| **80–100** | 現有告警品質良好。評估是否需要 Dynamic Alerting 的治理、多租戶能力 |
| **50–79** | 存在改善空間。建議逐步遷移 WARN/BAD 告警，享受 auto-suppression + scheduled thresholds |
| **0–49** | 告警品質需系統性改造。建議進入完整 Shadow Monitoring → Cutover 流程（Phase 1–6） |

可選：將品質掃描納入 CI（每週 cron job），追蹤告警品質趨勢。

---

## 問題

組織正在從傳統告警系統遷移到 Dynamic Alerting 平台，面臨的核心風險：規則行為差異、無停機轉換需求、驗證期長（1–2 週）、多系統聯動容易出錯。

## 解決方案：Shadow Monitoring 全自動切換

Dynamic Alerting 提供端到端的遷移工作流，包含：

1. **合規性掃描**（`onboard_platform.py`）— 解析舊的 Alertmanager 設定與規則檔，產出路由片段與遷移計畫
2. **規則轉換**（`migrate_rule.py`）— 舊規則 → 平台的 recording／alert rule 與租戶配置，alert rule 帶 `migration_status: shadow` label
3. **並行驗證**（`validate_migration.py`）— 連續比對新舊規則輸出，直到自動檢測到收斂
4. **一鍵切換**（`cutover_tenant.py`）— 依序停止 shadow monitor job、刪除舊 recording rule、移除 shadow label 與 Alertmanager 攔截，最後確認租戶的閾值指標存在
5. **回退** — 工具沒有自動回退；切換失敗時照 [Shadow Monitoring SOP §7.2](../shadow-monitoring-sop.md) 手動恢復（`cutover_tenant.py` 失敗時也會印出這個指引）

## 工作流程圖

```mermaid
graph LR
    A["現有告警<br/>（舊系統）"] -->|導入| B["onboard_platform.py<br/>反向分析"]
    B -->|產出| C["migration-plan.csv<br/>遷移計畫"]
    C -->|參考| D["migrate_rule.py<br/>規則轉換"]
    D -->|部署| E["新規則<br/>migration_status: shadow<br/>（被 AM 攔截）"]
    A -->|並行運行| E
    E -->|驗證| F["validate_migration.py<br/>--watch 持續監測"]
    F -->|7天+ 無差異| G["cutover-readiness.json<br/>自動產出<br/>（auto-detect-convergence）"]
    G -->|手動確認| H["cutover_tenant.py<br/>--readiness-json<br/>--dry-run"]
    H -->|preview| I{確認無誤？}
    I -->|是| J["cutover_tenant.py<br/>執行切換"]
    I -->|否| K["修改配置<br/>重新驗證"]
    K -->|再驗| F
    J -->|Step 1–2| L["停止 Shadow Monitor Job<br/>刪除舊 Recording Rules"]
    L -->|Step 3| M["移除 migration_status<br/>shadow label"]
    M -->|Step 4| N["Alertmanager 移除<br/>shadow 攔截 route"]
    N -->|Step 5| O["確認租戶閾值指標存在<br/>count(user_threshold)"]
    O -->|通過| P["完成切換 ✓"]
    O -->|失敗| Q["手動回退<br/>（SOP §7.2）"]
    Q -->|恢復| E
```

## 關鍵決策點

### 1. 收斂偵測（Convergence Detection）

新舊規則「行為等價」由以下條件判定：

| 條件 | 驗證方式 | 說明 |
|------|--------|------|
| **數值誤差** | delta < tolerance（預設 0.1%） | rate-based metrics 可適度放寬至 1% |
| **連續週期** | 連續 7 天 0 mismatch | 涵蓋週一至週日完整業務週期 |
| **峰谷覆蓋** | 時間戳跨越業務高峰+低谷 | 確保 scaling 場景也通過驗證 |
| **所有 tenant** | 每個 tenant 至少有驗證數據 | 無遺漏租戶 |
| **運營模式** | normal（非 silent/maintenance） | 確保比對期間平台正常運行 |

**自動收斂偵測**：

```bash
# validate_migration.py 自動偵測收斂並產出 cutover-readiness.json
python3 scripts/tools/ops/validate_migration.py \
  --mapping migration_output/prefix-mapping.yaml \
  --prometheus http://localhost:9090 \
  --watch --interval 300 --rounds 4032 \
  --auto-detect-convergence --stability-window 5
  # stability-window=5 表示連續 5 輪無 mismatch 即宣告收斂
```

輸出：`validation_output/cutover-readiness.json`（包含 `ready`、`timestamp`、`convergence_percentage`、`converged_count`、`total_pairs`、`unconverged_pairs`、`recommendation` 等欄位）。可用 `da-tools shadow-verify convergence --readiness-json <path>` 自動解讀。

### 2. 容忍度閾值（Tolerance Thresholds）

不同類型的指標應設置差異化的容忍度：

| 指標類型 | 預設容忍度 | 調整場景 |
|---------|----------|--------|
| 絕對值（connections、threads） | 0.1% | 極少調整 |
| Rate（QPS、throughput） | 1% | 波動較大，可放寬 |
| Percentile（p95 latency） | 5% | 尖峰敏感，容忍度較高 |
| Ratio（利用率 %） | 0.5% | 中等敏感 |

在 `validate_migration.py` 中傳入 `--tolerance` 參數：

```bash
python3 scripts/tools/ops/validate_migration.py \
  --mapping migration_output/prefix-mapping.yaml \
  --prometheus http://localhost:9090 \
  --watch \
  --tolerance 0.01  # 1% 容忍度
```

### 3. 回退條件（Rollback Triggers）

切換後若檢測到以下情況，立即依 [SOP §7.2](../shadow-monitoring-sop.md) 手動回退（工具沒有自動回退）：

| 條件 | 檢測方式 | 回退操作 |
|------|--------|--------|
| **Alert 誤報** | `check-alert` 返回 firing state mismatch | 恢復舊規則 + 重啟 validate |
| **通知失敗** | Alertmanager 通知隊列堆積或 webhook 失敗 | 恢復 AM 舊設定 |
| **Tenant 模式異常** | `diagnose` 發現 operational_mode ≠ normal | 中止切換，待恢復後重試 |
| **指標缺失** | 新規則產出的 metric 突然斷檔 | 恢復舊規則，檢查 Prometheus 狀態 |

## 逐步工作流

### 階段 1：準備（Day -1）

```bash
# 1.1 備份現有告警配置
cp -r conf.d conf.d.bak
cp -r alertmanager.yml alertmanager.yml.bak

# 1.2 掃描並分析現有配置
python3 scripts/tools/ops/onboard_platform.py \
  --alertmanager-config alertmanager.yml \
  --rule-files '/path/to/old_rules/*.yaml' \
  --output-dir migration_input/
# glob 要加引號，否則 shell 先展開成多個檔名，rc=2
# 產出（migration_input/）：
#   - onboard-hints.json（租戶清單、路由提示、DB 類型；Alertmanager 設定裡
#     找得到租戶 route 才會寫）
#   - phase1-routing/：每個租戶 route 的路由摘要與 <租戶>.yaml 路由片段
#   - phase2-rules/migration-plan.csv（每條舊告警規則的 metric、閾值、建議聚合方式，
#     以及 status：perfect 可直接轉換／complex 需人工確認／unparseable 無法解析）
#   - phase2-rules/_defaults-suggestion.yaml（由舊閾值推得的平台預設值建議；
#     含 complex 規則的閾值，合併前要逐條對過）

# 1.3 驗證環境就緒
python3 scripts/tools/ops/validate_config.py \
  --config-dir conf.d/ \
  --policy .github/custom-rule-policy.yaml
```

### 階段 2：轉換（Day 0）

```bash
# 2.1 執行規則轉換（輸入是舊的 Prometheus 規則檔，一次一個檔）
python3 scripts/tools/ops/migrate_rule.py /path/to/old_rules/alerts.yaml \
  --output-dir migration_output/
# 產出（migration_output/）：
#   - platform-recording-rules.yaml、platform-alert-rules.yaml
#     （新規則；alert rule 帶 migration_status: shadow label）
#   - defaults-snippet.yaml（先合併進 _defaults.yaml 的 defaults: 區塊；exporter 只發射
#     宣告過的 key。值取自原規則，warning 層對所有租戶生效）
#   - tenant-config.yaml（warning 層只有要和預設值不同的租戶才需要；<key>_critical
#     不能放 defaults，要 critical 的租戶都要貼進自己的 conf.d 檔；只有 critical 的
#     舊規則例外，它改讀 base 列，值已在 defaults-snippet.yaml）
#   - prefix-mapping.yaml（階段 3 validate_migration 的比對組）
#   - migration-report.txt、triage-report.csv（轉換報告）
# 工具沒有「只轉某些租戶」的選項；某個租戶不要這條告警，在該租戶檔把 key 設成 "disable"。

# 2.2 部署新規則（shadow 狀態）：把兩份規則檔合併進 Prometheus 的規則 ConfigMap
#     （具體操作依環境：ConfigMap 或 Helm）

# 2.3 更新 Alertmanager，攔截 shadow alert
kubectl patch configmap alertmanager-config -n monitoring \
  --patch-file alertmanager-shadow-route.patch

# 2.4 reload Prometheus 和 Alertmanager
kubectl rollout restart deployment prometheus -n monitoring
kubectl rollout restart deployment alertmanager -n monitoring
```

### 階段 3：驗證（Day 1–14）

```bash
# 3.1 啟動並行驗證
python3 scripts/tools/ops/validate_migration.py \
  --mapping migration_output/prefix-mapping.yaml \
  --prometheus http://localhost:9090 \
  --watch --interval 300 --rounds 4032 \
  --auto-detect-convergence --stability-window 7 \
  -o validation_output/

# 3.2 日常巡檢（每日一次，使用 shadow-verify 自動化）
da-tools shadow-verify runtime \
  --report-csv validation_output/validation-report.csv \
  --prometheus http://localhost:9090
# 若有 mismatch，參見 shadow-monitoring-sop.md §5 異常處理
```

### 階段 4：切換前確認（Day 14+）

```bash
# 4.1 收斂驗證
da-tools shadow-verify convergence \
  --report-csv validation_output/validation-report.csv \
  --readiness-json validation_output/cutover-readiness.json \
  --prometheus http://localhost:9090

# 4.2 乾運行模式（--dry-run）
python3 scripts/tools/ops/cutover_tenant.py \
  --readiness-json validation_output/cutover-readiness.json \
  --tenant db-a \
  --prometheus http://localhost:9090 \
  --dry-run

# 預期輸出（stderr；列出會執行的 kubectl 命令，不做任何變更）：
# ▸ Stop Shadow Monitor Job...
#   [dry-run] kubectl delete job shadow-monitor -n monitoring --ignore-not-found=true
#   ✓ (dry-run)
# ▸ Remove old Recording Rules...
#   [dry-run] kubectl delete configmap prometheus-rules-old -n monitoring --ignore-not-found=true
#   ✓ (dry-run)
# ▸ Remove shadow label from rules...
#   [dry-run] kubectl label configmap prometheus-rules -n monitoring migration_status-
#   ✓ (dry-run)
# ▸ Remove Alertmanager shadow route...
#   [dry-run] kubectl label configmap alertmanager-config -n monitoring migration_status-
#   ✓ (dry-run)
# ▸ Verify tenant health...
#   [dry-run] query http://localhost:9090 for tenant=db-a health
#   ✓ (dry-run)
#
# ✅ Cutover completed successfully.
# Next: run 'da-tools batch-diagnose' for full health report.

# 4.3 確認預覽無誤後執行切換
```

### 階段 5：切換執行（Day 14+）

```bash
# 5.1 執行單個 tenant 切換
python3 scripts/tools/ops/cutover_tenant.py \
  --readiness-json validation_output/cutover-readiness.json \
  --tenant db-a \
  --prometheus http://localhost:9090

# 預期流程（自動執行，同 4.2 的五步，這次真的執行 kubectl）：
# ▸ Stop Shadow Monitor Job...
# ▸ Remove old Recording Rules...
# ▸ Remove shadow label from rules...
# ▸ Remove Alertmanager shadow route...
# ▸ Verify tenant health...
#   ✓ tenant=db-a: <N> threshold metrics active
#
# ✅ Cutover completed successfully.
# 任一步失敗時印出 ❌ Cutover failed at step: <步驟> 與原因，並指向 SOP §7.2 的回退步驟。

# 5.2 批次切換多個 tenant（逐一執行）
for tenant in db-a db-b db-c; do
  echo "[INFO] Cutting over $tenant..."
  python3 scripts/tools/ops/cutover_tenant.py \
    --readiness-json validation_output/cutover-readiness.json \
    --tenant "$tenant" \
    --prometheus http://localhost:9090

  sleep 60  # 每個 tenant 間隔 60 秒，避免 Prometheus reload 沖突
done

# 5.3 驗證全部切換成功（多租戶健康報告）
python3 scripts/tools/ops/batch_diagnose.py \
  --tenants db-a,db-b,db-c \
  --prometheus http://localhost:9090
```

### 階段 6：清理（Day 15+）

```bash
# 6.1 確認舊規則已完全移除：cutover 刪掉的兩個物件應該都查不到（NotFound）
#     batch-diagnose 沒有 shadow 殘留檢查（尚未實作），用 kubectl 直接查
kubectl get job shadow-monitor -n monitoring
kubectl get configmap prometheus-rules-old -n monitoring

# 6.2 清理遷移產物與備份
rm -rf migration_input/ migration_output/ validation_output/
rm -rf conf.d.bak alertmanager.yml.bak
```

## 常見情況與應對

### 情況 1：驗證期間發現數值 Mismatch

**症狀**：`validation-report.csv` 中持續出現 `mismatch` 項目

**診斷與修復**：詳見 [Shadow Monitoring SOP §5](../shadow-monitoring-sop.md) 的完整 Mismatch/Missing 診斷表。常見原因包括聚合邏輯差異、label 不匹配、評估窗口差異。修復後需重新轉換規則並重啟驗證（新一輪 watch + 7 天無 mismatch）。

### 情況 2：切換後 Alert 仍未觸發

**症狀**：`check-alert` 返回 `no active alerts`

**常見原因與修復**：

| 原因 | 修復 |
|------|------|
| 新規則未被 Prometheus 載入 | 確認 ConfigMap reload 完成，等待 1–2 個 eval interval |
| Alertmanager route 還在攔截 | 檢查 shadow route 是否被完全移除 |
| Tenant 處於 silent/maintenance 模式 | 等待 `expires` 期滿自動恢復，或手動清除 |
| 閾值設置過高 | 使用 `baseline_discovery.py` 重新建議閾值 |

使用 `da-tools diagnose <tenant>` 快速確認運營模式與 exporter 狀態。

### 情況 3：需要快速回退

**操作**：

```bash
# 3.1 立即停止新規則通知（暫時方案）
kubectl patch configmap alertmanager-config -n monitoring \
  --patch-file alertmanager-block-custom.patch  # 臨時攔截 custom_* alerts

# 3.2 執行完整回退：cutover 沒有回退選項（尚未實作），照 shadow-monitoring-sop.md
#     §7.2 手動恢復舊 recording rule 與 Alertmanager 設定，再重啟 validate_migration

# 3.3 根本原因分析
# - 檢查新規則邏輯是否有誤
# - 檢查 threshold-exporter 配置是否正確
# - 檢查 Rule Pack 是否有衝突
```

## 真實案例與時間參考

### 案例 1：標準 Single-Tenant 遷移（DB-A 租戶）

| 階段 | 工作內容 | 耗時 | 備註 |
|------|--------|------|------|
| 準備 | onboard + migrate | 2h | 首次遷移，包含工具學習曲線 |
| 驗證 | 7 天連續監測 | 7d | 跨越完整業務週期 |
| 切換 | cutover + 驗證 | 30m | 全自動執行 |
| 清理 | 移除產物 + 最終檢驗 | 1h | 包含備份驗證 |
| **總計** | | **7.5 天** | |

### 案例 2：多租戶批次遷移（DB-A、DB-B、DB-C）

| 階段 | 耗時 | 說明 |
|------|------|------|
| 統一準備（Day -1） | 2h | 一次性 onboard；multiple migrate（平行可行但需序列執行 AM patch） |
| 統一驗證（Day 1–7） | 7d | 所有 tenant 在同一 validate job 中並行監測 |
| 批次切換（Day 8） | 1.5h | 3 × 30m，tenant 間隔 1 分鐘 |
| **總計** | **7.5 天** | 與單 tenant 相同（驗證週期支配） |

### 案例 3：發現 Mismatch 並修復（Day 3 異常）

| 操作 | 耗時 |
|------|------|
| 發現 mismatch（監測） | 自動 |
| 診斷根本原因 | 2h |
| 調整 migrate 參數 + 重新轉換 | 1h |
| 重啟驗證 | 7d |
| **總額外耗時** | **7 天** |

## 高級選項

### Option A: Accelerated Validation (--tolerance relaxed + --stability-window shortened)

若組織願意接受更高風險，可加速驗證期：

```bash
# 容忍度提寬至 5%，連續 3 輪無 mismatch 即宣告收斂
python3 scripts/tools/ops/validate_migration.py \
  --mapping migration_output/prefix-mapping.yaml \
  --prometheus http://localhost:9090 \
  --watch --interval 300 --rounds 288 \
  --auto-detect-convergence --stability-window 3 \
  --tolerance 0.05

# 驗證週期從 14 天降至 ~3 天
# 風險：rate-based metrics 容易出現短期波動誤報
```

**何時使用**：測試環境、低風險租戶（如開發環境）

### Option B: --force Skip Readiness Check

在確保手工驗證充分的情況下，可略過 readiness 判定：

```bash
# readiness JSON 顯示 ready: false 也照樣切換
python3 scripts/tools/ops/cutover_tenant.py \
  --readiness-json validation_output/cutover-readiness.json \
  --tenant db-a \
  --prometheus http://localhost:9090 \
  --force
```

`--force` 只略過 `ready` 的判定，`--readiness-json` 仍然必填：不給會直接以 rc=2 結束（`the following arguments are required: --readiness-json`）。

**何時使用**：已手工審視 CSV 報告確認 7 天無 mismatch；測試/開發環境

**何時避免**：生產環境、關鍵租戶

## 檢查清單

遷移前確保完成：

- [ ] 現有配置已備份（`conf.d.bak`, `alertmanager.yml.bak`）
- [ ] 執行 `validate_config.py` 通過
- [ ] 執行 `onboard_platform.py` 完成，產出（`phase2-rules/migration-plan.csv` 等）已審視
- [ ] 執行 `migrate_rule.py` 完成，新規則已部署
- [ ] Alertmanager shadow route 已部署
- [ ] Prometheus reload 完成
- [ ] `validate_migration.py` 正常運行，無錯誤日誌

切換前確保完成：

- [ ] `cutover-readiness.json` 已產出（或手工確認 7 天無 mismatch）
- [ ] `--dry-run` 預覽無異常
- [ ] Pagerduty/Slack 通知管道已測試（避免切換期間丟失告警）
- [ ] 待命 SRE 已確認，能於 1 小時內進行回退

切換後確保完成：

- [ ] `check-alert` 驗證通過
- [ ] `diagnose` 確認無異常
- [ ] 舊規則已完全移除
- [ ] 備份已歸檔

## 互動工具

> 💡 **互動工具** — 下列工具可直接在 [Interactive Tools Hub](https://vencil.github.io/Dynamic-Alerting-Integrations/) 中測試：
>
> - [Migration Simulator](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/migration-simulator.jsx) — 預覽遷移過程和驗收結果
> - [Health Dashboard](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/health-dashboard.jsx) — 監控遷移期間的系統健康狀況
> - [Config Diff](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/config-diff.jsx) — 比對遷移前後的告警規則配置

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [Shadow Monitoring SRE SOP](../shadow-monitoring-sop.md) | ⭐⭐⭐ |
| [Migration Guide](../migration-guide.md) | ⭐⭐⭐ |
| [da-tools CLI Reference](../cli-reference.md) | ⭐⭐ |
| [Grafana Dashboard 導覽](../grafana-dashboards.md) | ⭐⭐ |
| [場景：租戶完整生命週期管理](tenant-lifecycle.md) | ⭐⭐ |
