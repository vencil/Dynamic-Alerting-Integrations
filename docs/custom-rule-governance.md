# 多租戶客製化規則治理規範 (Custom Rule Governance Model)

> **受眾**: Platform Engineering、Domain Experts (DBA/Infra)、Tenant Tech Leads
> **版本**: v0.12.0
> **相關文件**: [架構與設計](architecture-and-design.md)、[規則包目錄](../rule-packs/README.md)、[遷移指南](migration-guide.md)

---

## 1. 目的

Dynamic Alerting 平台的核心價值在於 **O(M) 複雜度**：定義 M 種指標類型一次，所有 Tenant 透過 Config 調整閾值，不需要每個 Tenant 維護自己的 PromQL。

然而在企業實務中，部分 Tenant 的告警需求無法單靠閾值差異涵蓋，可能涉及不同的條件組合或業務場景。本規範定義三層治理模型，在保持 O(M) 效能優勢的前提下，提供結構化的客製化路徑。

---

## 2. 三層治理模型

### 2.1 Tier 1 — Standard（Config-Driven 三態控制）

**覆蓋率**: 約 80–85% 的 Tenant 需求

Tenant 透過 `tenant.yaml` 設定閾值，不接觸 PromQL：

```yaml
# 三態控制範例
connections_threshold: "800"        # Custom: 自訂閾值
cpu_threshold: ""                   # Default: 採用平台預設值 (省略或空字串)
replication_lag_threshold: "disable" # Disable: 關閉此告警
```

每個指標支援 Warning / Critical 兩層嚴重度（`_critical` suffix），以及維度標籤篩選。

**Rule 複雜度**: O(M)，不隨 Tenant 數成長。

### 2.2 Tier 2 — Pre-packaged Scenarios（預製複合場景）

**覆蓋率**: 約 10–15% 的 Tenant 需求

由 Domain Expert 根據實戰經驗，預先定義具備明確業務語義的複合告警場景。Tenant 不需要撰寫 PromQL，僅決定是否啟用該場景並調整參數。

**現有範例 — `MariaDBSystemBottleneck`**：

```yaml
# 業務語義：連線數與 CPU 同時超標 = 真實負載瓶頸（非 connection leak）
- alert: MariaDBSystemBottleneck
  expr: |
    (
      tenant:mysql_threads_connected:sum
      > on(tenant) group_left
      tenant:alert_threshold:connections
    )
    and on(tenant)
    (
      tenant:mysql_cpu_usage:rate5m
      > on(tenant) group_left
      tenant:alert_threshold:cpu
    )
    unless on(tenant)
    (user_state_filter{filter="maintenance"} == 1)
```

Tenant 透過三態控制啟停：

```yaml
_state_mariadb_bottleneck: "true"     # 啟用
_state_mariadb_bottleneck: "disable"  # 停用
```

**設計原則**：

- Tier 2 場景由 Domain Expert 定義，不是由 Tenant 自行拼裝。「平台提供精選套餐，Tenant 決定要不要點」，而非「給 Tenant 積木自己拼」。
- 每個場景必須有明確的業務語義文件（回答什麼業務問題、為什麼這個組合有意義）。
- 閾值仍然是 Config-driven，Tenant 可調整數字但不能改變邏輯結構。
- PromQL 中不存在動態指標名稱替換的能力，因此每個場景的指標組合在 Rule 載入時即確定。

**Rule 複雜度**: O(場景數)，不隨 Tenant 數成長。

### 2.3 Tier 3 — True Custom（嚴格治理的客製化區）

**覆蓋率**: 不超過全部 Rule 的 5%

用於 Tier 1 + Tier 2 無法滿足的例外需求。必須經過正式的 Change Request 流程。

**准入條件**：

1. 提交者必須說明為什麼 Tier 1（閾值調整）和 Tier 2（預製場景）無法滿足需求
2. Domain Expert 審查後判斷是否值得抽象為新的 Tier 2 場景
3. 通過 CI deny-list linting（見 §4）
4. 標注 owner label 與 expiry date

**架構隔離**：

Tier 3 規則放置於獨立的 Prometheus Rule Group，可設定較長的 `evaluation_interval`（例如 30s 而非預設 15s）。這確保：

- 如果某條 Custom Rule 的 PromQL 過重導致 evaluation 延遲，影響範圍被隔離在該 group 內
- Tier 1 和 Tier 2 的告警時效性不受 Noisy Neighbor 影響
- 平台團隊可獨立監控 Custom Rule Group 的 evaluation duration

```yaml
# rule-packs/custom/tenant-specific.yaml
groups:
  - name: custom_tenant_rules
    interval: 30s   # 獨立 evaluation interval
    rules:
      - alert: CustomAlert_db-a_special_tablespace
        expr: |
          custom_tablespace_usage{tenant="db-a", tablespace="SPECIAL_APP"} > 95
        labels:
          tier: "custom"
          owner: "team-db-a"
          expiry: "2026-06-30"
        annotations:
          ticket: "REQ-12345"
          justification: "Tier 2 無對應場景：單一特殊 tablespace 的獨立閾值"
```

**Rule 複雜度**: O(Custom Rule 數)。管理目標是控制在全部 Rule 的 5% 以內。

---

## 3. 權責定義 (RnR)

本平台涉及三種角色。在小型團隊中同一人可能兼任多個角色，重點是職責邊界清楚，不是組織架構。

### 3.1 Platform Engineering / Core SRE

**定位**: 基礎設施的提供者與護欄的建立者

| 職責 | 說明 |
|------|------|
| 平台可用性 | 維護 Prometheus 叢集、threshold-exporter HA、Projected Volume 掛載 |
| CI/CD 護欄 | 維護 deny-list linting、版號治理、Rule Pack 結構驗證 |
| 效能監控 | 監控 Rule evaluation duration，識別 Noisy Neighbor |
| 強制下架權 | 對違規或導致效能問題的 Tier 3 Rule，有權在不事先通知的情況下強制停用以保全局 |

**SLA 範圍**: 保證「告警引擎」正常運作（Rule evaluation、metric scraping、alert routing）。不對特定業務指標的誤報/漏報負責。

### 3.2 Domain Experts（DBA、網路管理員、K8s 管理員）

**定位**: 黃金標準 (Golden Standards) 的制定者

| 職責 | 說明 |
|------|------|
| Rule Pack 維護 | 各自負責所屬領域的 Rule Pack（例如 DBA 負責 mariadb rule-pack） |
| Tier 2 場景設計 | 根據實戰經驗設計 Pre-packaged Scenarios，撰寫業務語義文件 |
| Tier 3 審查 | 審查 Tenant 提出的 Custom Rule 需求，判斷是否應抽象為 Tier 2 |
| 收編週期 | 參與季度 review，將具備共性的 Tier 3 Rule 晉升為 Tier 2 |

**SLA 範圍**: 對 Tier 1 / Tier 2 Rule 的業務正確性負責（閾值合理性、場景設計邏輯）。

### 3.3 Tenant Teams（應用程式開發團隊）

**定位**: 平台的使用者與自身業務系統的負責人

| 職責 | 說明 |
|------|------|
| 閾值管理 | 透過 `tenant.yaml` 維護自己服務的 Warning / Critical 閾值 |
| 場景選擇 | 決定是否啟用 Tier 2 的 Pre-packaged Scenarios |
| Custom Rule 維運 | 若提交 Tier 3 Rule，遵循 "You build it, you run it" 原則 |

**SLA 說明**: Tier 3 Custom Rule 不保證 SLA。如果 Tenant 自行提出的 Custom Rule 導致誤報，平台團隊不會在非工作時間處理，排入 Tenant 自己的 Ticket Queue。

> **實務補充**: Tenant 團隊通常不具備撰寫 PromQL 的能力。實際流程中，Tenant 提出需求，Domain Expert 評估後代為撰寫，但 SLA 歸屬仍回到 Tenant — 即「Domain Expert 幫你寫，但告警品質由你負責」。

### 3.4 責任歸屬速查表

| 情境 | 責任歸屬 |
|------|---------|
| Prometheus 掛掉，所有告警失效 | Platform Engineering |
| Tier 1 閾值太低導致誤報 | Tenant（閾值由 Tenant 自行設定） |
| Tier 2 場景邏輯設計不當，導致該場景全域漏報 | Domain Expert |
| Tier 3 Custom Rule 語法太重，拖慢 evaluation | Platform Engineering 強制下架 → Tenant 修正後重新提交 |
| Tier 3 Custom Rule 誤報 | Tenant |

---

## 4. CI 護欄：Deny-list Linting

所有提交至 `rule-packs/custom/` 的 Rule 必須通過自動化檢查。

### 4.1 Deny-list 規則

```yaml
# .github/custom-rule-policy.yaml
denied_functions:
  - holt_winters           # CPU 密集型函式
  - predict_linear         # 大範圍回溯查詢
  - quantile_over_time     # 高記憶體消耗

denied_patterns:
  - '=~".*"'              # 全通配 regex（效能殺手）
  - 'without(tenant)'     # 破壞 tenant 隔離

required_labels:
  - tenant                 # 所有 Custom Rule 必須帶 tenant label

max_range_duration: 1h     # 禁止 [7d] 等超長 range vector
max_evaluation_interval: 60s  # Custom Rule group interval 上限
```

### 4.2 Linting 工具

```bash
# CI 中執行
python3 scripts/tools/lint_custom_rules.py rule-packs/custom/

# 使用自訂 policy
python3 scripts/tools/lint_custom_rules.py rule-packs/custom/ --policy .github/custom-rule-policy.yaml

# 透過 da-tools 容器執行（不需 clone 專案）
docker run --rm \
  -v $(pwd)/my-custom-rules:/data/rules \
  ghcr.io/vencil/da-tools:0.3.0 \
  lint /data/rules --ci

# 輸出範例
# PASS: custom_tenant_rules.yaml - 2 rules checked
# FAIL: bad_rule.yaml:15 - denied function 'holt_winters' in expr
# FAIL: bad_rule.yaml:22 - missing required label 'tenant'
```

### 4.3 為什麼限制「重量」而非「數量」

固定 Quota（如「每個 Tenant 5 條」）的問題：

- **浪費與不足並存**: 有的 Tenant 一條都不用，有的 Tenant 第 6 條需求來了就卡住
- **Rule 爆炸未解決**: 50 Tenant × 5 條 = 250 條，每條邏輯不同，維護成本反而更高
- **空轉成本**: Prometheus 每 15 秒 evaluate 所有 Rule，不管是否有 Tenant 在用

Deny-list 方式限制的是每條 Rule 的「計算重量」，而非 Rule 數量。搭配獨立 Rule Group 隔離和 evaluation duration 監控，能在不設硬上限的情況下防止效能劣化。

---

## 5. 收編與晉升機制 (Assimilation Cycle)

### 5.1 週期

每季進行一次 Custom Rule Review（建議與季度 SLA Review 合併）。

### 5.2 流程

```
Tier 3 Custom Rule
    │
    ├─ 多個 Tenant 提出相似需求？
    │   └─ YES → Domain Expert 評估抽象為 Tier 2 Pre-packaged Scenario
    │            → 撰寫業務語義文件
    │            → 移入對應 Rule Pack
    │            → 原 Tier 3 Rule 標記 deprecated，設定 expiry
    │
    ├─ 已過 expiry date？
    │   └─ YES → 通知 Tenant owner
    │            → 14 天內未回應 → 自動停用
    │
    └─ evaluation duration 持續偏高？
        └─ YES → Platform Engineering 通知 Tenant
                 → 30 天內未優化 → 強制下架
```

### 5.3 健康度指標

建議在 threshold-exporter 中追蹤：

```
# Custom Rule 數量分佈
da_custom_rule_count{tenant="db-a", tier="2"} 3
da_custom_rule_count{tenant="db-a", tier="3"} 1

# Tier 3 佔比超過 5% 時觸發告警
da_custom_rule_ratio_tier3 > 0.05
```

如果特定 Tenant 的 Tier 3 count 持續上升，這是一個信號：可能是 Tier 2 場景設計需要擴充，而非 Tenant 需求過於特殊。

---

## 6. 快速參考

### Tier 對照表

| | Tier 1 (Standard) | Tier 2 (Pre-packaged) | Tier 3 (Custom) |
|---|---|---|---|
| **控制方式** | tenant.yaml 閾值 | 三態啟停 + 參數 | 完整 PromQL |
| **撰寫者** | Tenant 自行設定 | Domain Expert 預製 | Domain Expert 代寫 |
| **SLA** | 平台保證 | 平台保證 | 不保證 |
| **Rule 複雜度** | O(M) | O(場景數) | O(Custom 數) |
| **CI 檢查** | 自動（三態驗證） | Rule Pack CI | Deny-list linting |
| **生命週期** | 永久 | 永久 | 帶 expiry date |

### Tenant 決策樹

```
我需要一個新的告警 →
  ├─ 現有指標 + 不同閾值？ → Tier 1: 修改 tenant.yaml
  ├─ 現有指標的複合條件？ → Tier 2: 檢查是否有對應 Pre-packaged Scenario
  │   ├─ 有 → 啟用該 Scenario
  │   └─ 沒有 → 向 Domain Expert 提出需求
  │       ├─ 具共性 → Domain Expert 建立新 Tier 2 Scenario
  │       └─ 高度特殊 → 進入 Tier 3 流程
  └─ 完全不同的指標來源？ → 評估是否為平台應涵蓋的範疇
```
