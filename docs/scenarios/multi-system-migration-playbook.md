---
title: "Multi-System Migration Playbook (Outline)"
tags: [migration, playbook, scenarios, multi-system, hybrid-format]
audience: [platform-engineers, sre, architects]
version: v2.7.0
lang: zh
---

# Multi-System Migration Playbook

> **Status**: 🟡 Outline（v0.1，2026-05-10）— 5-Phase 結構、決策樹、Gate 模型、Schema 都已 locked from PR #375-#388 strategic discussion；本檔提供 ToC + 每段 design intent + checklist 骨架。Phase-by-phase 內文在後續 PR 補完。
>
> **適用情境**：客戶同時換 storage backend (Prom→VM)、規則層、AM routing，並追加平台的 `_defaults.yaml` metric-split feature。**不適合**：greenfield / 1-system / 2-system → 走決策樹下方對應 redirect。
>
> **語氣假設**：本 playbook 假設客戶已有成熟 Prometheus + Alertmanager 運維。**不教 Prometheus 基礎**；映射客戶既有概念到本平台。

---

## 0. 三層 reading speed（怎麼讀本文）

每個 Phase 同樣結構，依角色挑層次：

| 你是誰 | 讀哪段 | 預估 |
|---|---|---|
| **Manager / 跨團隊溝通**（broadcast 用）| 每 Phase 開頭的 「30 秒 TL;DR」（3 bullets） | 整 playbook < 5 分鐘 |
| **Architect / SRE Lead**（決策用）| 30 秒 TL;DR + Architect Narrative + Gates + Decision Trees | 整 playbook ~30 分鐘 |
| **On-call / Executor**（凌晨 cutover 用）| 跳到「Cutover Checklist」`<details>` + bash code blocks | 單 Phase < 5 分鐘 |

設計動機：**讀者不該抽自己的 TL;DR**。我們先抽好；不該跑命令的人不會看到命令（折疊預設關）；該執行的人 copy-paste 即可。

---

## 1. 我是哪一型客戶？（Routing Decision Tree）

```mermaid
flowchart TD
    Q1{現況描述哪一類？}
    Q1 -->|沒既有 rules、剛裝 Prom| GS["[for-tenants.md](../getting-started/for-tenants.md)<br/>Greenfield onboarding"]
    Q1 -->|有 rules、留 Prom infra、不換 AM| MG1["[migration-guide.md](../migration-guide.md)<br/>Rule import (1-system)"]
    Q1 -->|完整 Prom+AM+rules、留 infra| MG2["[migration-guide.md](../migration-guide.md) §rule+AM<br/>Rule + AM migration (2-system)"]
    Q1 -->|<b>Prom→VM + rules + AM 同時換</b>| THIS["✅ <b>本 playbook</b><br/>3-system migration"]

    style THIS fill:#dff,stroke:#066
```

如果你不確定該走哪一條，問自己：「**底層 storage 換不換？**」 換 → 本 playbook；不換 → migration-guide.md。

---

## 2. 5-Phase 全景

```mermaid
sequenceDiagram
    participant D as Discovery
    participant P as Pre-flight
    participant S as Shadow
    participant C as Cutover
    participant K as Decommission
    Note over D: Tier A 靜態 audit (hard)<br/>Tier B/C bonus
    D->>P: Gate 1
    Note over P: VM 起、雙寫、舊 Prom 留
    P->>S: Gate 2
    Note over S: 規則上 git，AM 帶 shadow label
    S->>C: Gate 3
    Note over C: Canary → 全量、git revert 保底
    C->>K: Gate 4 + 5
    Note over K: 關舊 Prom、清理、_defaults 漸進
```

5 個 Gate 全部用 **invariants**（不是「告警量一致」）—— 詳見 §10。

---

## 3. Phase 0 — Discovery & Inventory

### 30 秒 TL;DR
- 三層 tier audit：A 靜態（hard gate）/ B live snapshot（soft）/ C 歷史 telemetry（bonus）
- 產出 dual：**`.da/migration-state.json`**（機器讀，後續 phase 自動化用）+ Markdown summary（給 PR description / 給人類）
- Schema 詳見 [migration-state.md](../schemas/migration-state.md)

### Architect Narrative

#### 為什麼 Phase 0 是 hard gate，不能跳

**「我以為我懂自己的 inventory」是企業 monitoring 最常見的錯覺**。5 年以上的 Prometheus deployment 累積：規則被 commit 但沒人記得目的、receiver 用的 webhook URL 對應的人離職了、tenant id 在某次 hotfix 直接 hardcode 進 PromQL 沒人移除、multi-region 部署規則因為團隊分工各自演化、Operator 遷移過程留下 ConfigMap + PrometheusRule CRD 雙寫遺跡。

Phase 0 強制做**一次徹底的盤點**——不是為了完美修復（那要 Phase 3 才做），而是為了讓客戶與我們對「即將遷移什麼」有共同 mental model。Discovery 跳過 = Phase 1+ 每一步都基於假設不基於事實。

#### 三層 Tier 為什麼這樣切分

客戶 telemetry 成熟度差異極大——從「只有 Prom 在線、無任何長期儲存」到「Thanos + ELK + Datadog」都有。一個 hard gate fits all 是不切實際。三層 tier 對應三種客戶現況：

- **Tier A — 靜態分析**（hard gate，所有客戶都能過）
  完全脫機分析 PromRule CRD / rules.yaml / vmalert ConfigMap 等規則檔本身。Catches: syntax errors、orphan rules（無對應 receiver）、rules without route、receivers unused、tenant id 違反 schema、跨檔重複定義。**典型 100 規則客戶 < 30 秒跑完**。output 為 `.da/migration-state.json` + Markdown summary（dual output 詳見 [migration-state.md](../schemas/migration-state.md)）。

- **Tier B — Live snapshot**（soft gate，~80% 客戶可達）
  對活著的 Prometheus 跑 `ALERTS{}` 抓「現在 firing 的告警集合」+ 對 AM 跑 `/api/v2/alerts/groups` 抓 active routes。回答「現在實際在叫的東西是什麼？」這個 Tier A 沒法回答的問題。Limits: 需要 Prom auth + reachable；某些 shops policy 不開查詢權；高 cardinality 客戶查詢可能 timeout。**Tier B 缺席不擋 Phase 1**。

- **Tier C — 歷史 telemetry**（bonus，~20% 客戶可達）
  對 Thanos / VM long-retention / ELK alert logs 跑「過去 N 天 alert fire 分布」「哪些規則一年沒觸發過」「哪些 receiver 收過 webhook」。**多數客戶沒這層 telemetry**——不擋；Shadow phase 會做 dynamic noise filtering 替代。

#### Output dual format 的由來

JSON (`.da/migration-state.json`) + Markdown summary 來自同一個 internal state。**不是兩份檔案分別維護，是一個 derive 兩種視圖**：

- JSON：機器讀。Phase 3 cutover candidate selector / CI gate / 後續 phase 自動 advance log 都依賴它
- Markdown：人類讀。貼進 PR description 給 reviewer / 客戶 stakeholder broadcast

**Per-cluster split** 是 Phase 0 起手就建立的慣例（見 [migration-state.md §Storage Layout](../schemas/migration-state.md)）。多 cluster 並行推進不同 phase 時，single-file 會 GitOps merge conflict 地獄；per-cluster `.da/state/<cluster>.json` 是預設姿勢。

#### Phase 0 通常的 surprise

客戶聽到 Phase 0 結果常見三種反應：

1. **「我們有那麼多 orphan rules？」** — 規則被 commit 但對應 receiver 已不在 AM config，Phase 0 一掃就出來，平均每 100 規則有 5-15 條 orphan
2. **「這個 tenant id 是 hardcode 的？」** — `instance="db-prod-1"` 之類的 PromQL 直接寫死，dev-rule #2 violation。Phase 1 之前必須改
3. **「receiver 那個 PagerDuty token 早不能用了」** — 客戶才發現好幾個月來某域 alert 根本沒人收到。屬於 Phase 0 額外的**意外修復**而非預期成果

### Cutover Checklist

<details>
<summary>📋 Phase 0 Checklist（給 executor）</summary>

- [ ] 跑 Tier A 靜態 audit
  ```bash
  da-tools onboard --analyze \
      --output .da/migration-state.json \
      --markdown-summary > migration-summary.md
  ```
- [ ] 把 Markdown summary 貼進 PR description（給 reviewer）
- [ ] 確認 Tier A hard gates 通過：
  - [ ] 沒 syntax error 的孤兒 rule
  - [ ] 每個 receiver 都有對應 routing entry
  - [ ] tenant id 命名與我們的 schema 相容（dev-rule #2）
- [ ] **可選** Tier B：對活的 Prom 跑 `ALERTS{}` snapshot
- [ ] **可選** Tier C：對 Thanos / VM-long-retention 跑歷史查詢
- [ ] commit `.da/migration-state.json` 進 customer GitOps repo
</details>

### Failure modes
- 「Tier A 卡在 syntax error」：常見於手寫 PromQL 用 VM-only 函數 → `da-parser --strict-promql` 標出
- 「Tier B 拉不到 ALERTS{}」：Prom 太久沒 alert 評估 / 或 query timeout → 接受 Tier A 即可推進

### Gate 1 → Phase 1
**通過條件**：Tier A 全 hard checks pass + `.da/migration-state.json` 已 commit。

---

## 4. Phase 1 — Pre-flight & Dual-Write Infrastructure

### 30 秒 TL;DR
- VM cluster 起來（vmagent / vmselect / vmstorage 或 vmsingle）
- 客戶舊 Prom + 新 VM **同時 scrape** 相同 targets（dual-write）
- exporter 在我們的 cluster 起、發 `user_threshold` metric

### Architect Narrative

#### VM topology 選擇

**起跑用 vmsingle，達某水位再升 vmcluster**——不要在 Phase 1 就上 cluster。

| Topology | 適用規模 | HA | 複雜度 |
|---|---|---|---|
| **vmsingle**（單 binary）| < 1M active series | ❌ 單點 | 低（單一 binary） |
| **vmcluster**（vmstorage + vmselect + vminsert）| 1M+ active series 或 multi-tenant 需求 | ✅ replica + horizontal scale | 高（3 component 部署 + replication factor 調校） |

實務經驗：客戶在 Phase 1 上 vmcluster 的，Phase 1 stuck 機率比 vmsingle 高 3-5 倍——原因不是 VM bug，而是客戶 ops 對 vmcluster 不熟悉，部署 + debug + tuning 認知負擔過大。**保留 vmcluster 升級為 Phase 4 後的 capacity planning 議題**。

vmsingle disk budget rule of thumb：**bytes-per-series-per-day × series count × retention days × 1.5 (overhead buffer)**。bytes-per-series-per-day 在我們 metric pattern 下約 8-15 bytes，可用 [VM 官方 calculator](https://docs.victoriametrics.com/Single-server-VictoriaMetrics.html#capacity-planning) 估算。

#### Dual-write 策略

兩條合理 path，依客戶 vmagent 是否願意動：

**Option 1 — vmagent fan-out**（推薦，多數客戶走這條）

客戶部署 vmagent，配置 `remote_write` 同時對舊 Prom 與新 VM。vmagent 對 fan-out 是 first-class（支援 retry / backoff / disk buffer）。

```yaml
remoteWrite:
  - url: "http://prometheus.monitoring.svc:9090/api/v1/write"  # legacy
  - url: "http://vminsert.vm.svc:8480/insert/0/prometheus"     # new
```

**Option 2 — Prom 端 federate**（適合不想動 vmagent 的客戶）

舊 Prom 加 `remote_write` 到新 VM。Prom 對 remote_write 也支援，但 retry 行為比 vmagent 差。

不論哪 option，**雙寫意味雙倍 scrape load**。Phase 1 開始前確認 target endpoints 撐得住——某些 client metric endpoint（如 redis exporter）對高頻 scrape 敏感。

#### 為什麼 exporter 在 Phase 1 起、AM 不接

threshold-exporter 在 Phase 1 部署，但**故意不接 AM**：

- **動機**：Phase 2 shadow 才有 `user_threshold` metric 可比對；不在 Phase 1 預先 ship 會 chicken-and-egg
- **副作用**：Phase 1 期 exporter 的 metric 純 collect，Prom 那邊 `user_threshold{...}` 會出現但無 alert 路徑。客戶 ops 看到時可能困惑——預先告知「此期間 metric 是 silent state，AM 接線在 Phase 2」

#### Cardinality budget watch

Dual-write 期間 cardinality 會臨時 doubling。Phase 1 Gate 1 invariant 包含「VM 與 Prom 同 metric 數量 ±5%」確認 dual-write 健康——但**這只檢查兩邊一致性**，不檢查容量上限。

**容量觀察重點**（Phase 1 結束前**至少**過一次）：

- VM `vm_data_size_bytes` 增速 vs disk capacity
- Prom `prometheus_tsdb_head_series` cardinality 是否爆量
- vmagent `vmagent_remotewrite_pending_data_bytes` 不該長期 > 0（>0 = 寫不完，可能 OOM 預兆）

### Cutover Checklist

<details>
<summary>📋 Phase 1 Checklist</summary>

**Pre-flight**
- [ ] 確認 VM topology 選擇（vmsingle 預設 / vmcluster 僅 multi-tenant 規模 + HA 需求）
- [ ] 計算 disk budget：bytes-per-series × series count × retention × 1.5 buffer
- [ ] 預估 cardinality doubling 在 staging Prom 是否超 budget

**部署**
- [ ] 部署 VM 至 staging cluster
- [ ] 配置 dual-write（vmagent fan-out 或 Prom remote_write）
- [ ] 部署 threshold-exporter 到 staging（**不接 AM**）
- [ ] 驗證 `user_threshold` metric 在 VM 可查（`vmselect ... /api/v1/query`）

**Gate 1 verification**
- [ ] VM 與 Prom 同 metric 數量 ±5% 內（跑一週）
- [ ] vmagent `pending_data_bytes` 持續 ≈ 0
- [ ] VM disk 增速符合估算
- [ ] dual-write ≥ 7 天無斷點
</details>

### Gate 2 → Phase 2
**通過條件**：dual-write ≥ 7 天無掉點 + Tier B live snapshot 比對 staging vs prod-Prom 無 cardinality drift。

---

## 5. Phase 2 — Shadow Deployment

### 30 秒 TL;DR
- 規則 commit 到 git（單一 SOT 或 base + overlay，依 [Plan A vs B](#8-plan-a-vs-plan-bgit-layout-選擇)）
- AM routing 全部帶 `migration_status: shadow` label，告警導 /dev/null 或 debug channel
- 既有舊 Prom + AM 仍在線、仍是 production source-of-truth

### Architect Narrative（待寫）
- 為什麼 shadow 不直接接 production AM：信心不足 / 客戶 ops 還沒培訓
- Plan A vs B 此 phase 落地差異
- Gate 2 invariants 由來：**subset overlap = 100%**（既有等價規則必觸發）+ **新增 alert 顯式 sign-off**（避免 noise 被當回歸）

### Cutover Checklist

<details>
<summary>📋 Phase 2 Checklist</summary>

- [ ] 規則 commit 進 git（用 [migrate-conf-d](../cli-reference.md) 從舊規則轉換）
- [ ] AM routing 加 shadow matcher：
  ```yaml
  route:
    routes:
      - matchers: [migration_status="shadow"]
        receiver: "null"
        continue: false
  ```
- [ ] `da-tools shadow-verify preflight` 通過
- [ ] Shadow 期 ≥ 2 週（Gate 3 前置）
- [ ] 任一週內 invariants 都 hold
</details>

### Gate 3 → Phase 3
**通過條件**：
1. **Subset overlap = 100%**：舊系統有觸發的條件，新系統必觸發（catastrophic 假陰性 0）
2. **新增 alert 顯式 sign-off**：客戶 ops 對每條額外 alert 點頭（確認不是 bug 雜訊）
3. CI / CD 對 `_metric_federation_policy.yaml` 等變動 sticky 報告無 unexpected delta

---

## 6. Phase 3 — Incremental Cutover

### 30 秒 TL;DR
- Canary tenant（5-10%）先切：在 **rule 配置檔**移除該 tenant 的 `migration_status: shadow` label → **rule evaluator (Prom / vmalert) reload** → 該 tenant 觸發的告警 payload 不再帶 shadow label → AM 既有 route table 自然把它送進 production receiver
- 24h-1 ops cycle 觀察 → 推全量
- Rollback path：**git revert** config commit → rule evaluator reload → shadow label 恢復 → 告警重新被 AM 既有 shadow matcher 路由到 /dev/null（< 5 分鐘）

### Architect Narrative（待寫）

**關鍵機制澄清**（避免常見誤解）：

> Phase 3 改的是**規則檔**（rule evaluator 端），不是 AM config。AM 既有的 route table（含 `migration_status="shadow"` matcher）**完全不變**。
>
> - **改動處**：rule 配置（Prom rules.yml / vmalert rule files）—— 拔除該 tenant 規則上的 `migration_status: shadow` label
> - **觸發 reload 的對象**：rule evaluator (Prometheus / vmalert)，**不是** Alertmanager
> - **AM 端的行為**：AM 收到不帶 shadow label 的 alert payload → 既有 route 的 shadow matcher 不 match → fall through 到 production receiver。AM config 完全沒動
>
> 這個分工是 Canary 之所以可行的原因：如果改 AM config 移除 shadow matcher，會一次影響所有 tenants（無法 canary）。改 per-tenant rule label 才能精準切 5-10% 子集。

**其他**：
- Canary 比例 + 觀察窗：5% × 24h 是 minimum；推薦 **跨 1 個 ops cycle (typically 1 week)** 抓 weekly / monthly alerts
- Connect：**staged adoption**（custom_ → golden）由獨立 [Staged Adoption Lifecycle](staged-adoption-guide.md) 處理；**本 Phase 不重複那段內容**，只做 cutover 的 label flip

### Cutover Checklist

<details>
<summary>📋 Phase 3 Checklist</summary>

**Canary 階段**
- [ ] 選擇 canary tenants（典型 5-10%）
- [ ] git commit：在 **rule 配置檔** 移除 canary tenants 的 `migration_status: shadow` label（**不是** AM config）
- [ ] **Rule evaluator (Prom / vmalert) reload**（自動 — 透過 GitOps reconcile 或 SIGHUP / `/-/reload`）—— **不是** AM reload
- [ ] 驗證：該 tenant 觸發的下一個 alert payload 不再帶 `migration_status: shadow`
- [ ] 24h 觀察期：alert 觸發率、receiver 響應、人為 incidents
- [ ] Gate 4 通過 → 推全量

**全量階段**
- [ ] git commit：移除剩餘 tenants 的 shadow label（rule 端）
- [ ] Rule evaluator reload
- [ ] 觀察 ≥ 1 ops cycle（推薦 1 week）
- [ ] Gate 5 通過 → Phase 4

**Rollback**
- [ ] git revert 對應 commit → rule evaluator reload → shadow label 恢復在 alert payload → AM 既有 shadow matcher 重新生效 → 告警再次路由到 /dev/null
- [ ] **可逆性界線**：見 §11（config 全可逆 / 監控狀態半可逆 / 資料層不可逆）

> **常見錯誤**：以為要改 AM config 移除 shadow matcher。**不要這麼做** —— 那會一次影響所有 tenants 無法 canary。
</details>

### Gate 4（canary）→ 全量
**通過條件**：Canary tenants 跨 24h 無 unexpected alert + 客戶 ops sign-off。

### Gate 5（全量）→ Phase 4
**通過條件**：全量切換 ≥ 1 ops cycle 無 incident。

---

## 7. Phase 4 — Decommission

### 30 秒 TL;DR
- 舊 Prom 進入 read-only（停 alerting evaluation）
- N 天 grace period 後關 Prom
- 漸進啟用 `_defaults.yaml` metric-split feature（連 [Staged Adoption Lifecycle](staged-adoption-guide.md)）

### Architect Narrative（待寫）
- 為什麼分 read-only → off 兩步：歷史 query 需求（compliance / SRE 回顧）
- Decommission 後才能啟用 metric-split：避免 Phase 3 期 noise 被歸因到新功能

### Cutover Checklist

<details>
<summary>📋 Phase 4 Checklist</summary>

- [ ] 舊 Prom 移除 alert.rules.yml（純 read-only，仍可 query）
- [ ] Grace period（建議 30 天）
- [ ] Prom shutdown
- [ ] 按 [Staged Adoption Lifecycle](staged-adoption-guide.md) 漸進啟用 `_defaults.yaml`
- [ ] 更新客戶 internal docs / runbooks
</details>

---

## 8. Plan A vs Plan B（Git layout 選擇）

### Plan A — Single SOT + Per-cluster Exporter Version Skew（**預設**）

`conf.d/` 是單一 Git 樹，所有 cluster 共用。差異透過該 cluster 部署的 threshold-exporter 版本決定該 cluster 在哪個 phase。

```
conf.d/
├─ _defaults.yaml          # v2.8+ exporter 讀；v2.7 silently ignore
├─ <domain>/
│  └─ <region>/
│     └─ <tenant>.yaml
```

**Forward-compat 已驗證**（PR #375 P0 check）：v2.7.0 exporter 對 v2.8.0 新欄位 graceful ignore（`yaml.Unmarshal` lenient + `_*` 底線檔案跳過慣例）。

**何時用 Plan A**：cluster 間版本差距 ≤ 1 minor、無 per-cluster selective feature 需求。Cover ~80% 客戶情境。

### Plan B — Base + Overlay（escape hatch）

```
conf.d/
├─ base/                  # 所有 cluster 共用
└─ overlays/
   ├─ staging/            # 進階 feature
   │  └─ _defaults.yaml
   └─ prod/               # 還在 Shadow
      └─ migration_status_routing.yaml
```

**何時用 Plan B**：客戶要 per-cluster selective feature adoption（staging 啟用 _defaults、prod 暫不啟用）。

**Plan B platform investment**：exporter 需要 multi-mount-point overlay merge 邏輯——**目前未 ship**，是 v2.9 backlog 項。客戶觸發 Plan B 時請先確認 platform team 排期。

---

## 9. Partial Migration（X-Y matrix）

5-Phase 是 **Y 軸**；scope wave 是 **X 軸**——兩者正交。

```
                 Phase 0  Phase 1  Phase 2  Phase 3  Phase 4
staging cluster   ✅       ✅       ✅       ✅       ✅
prod canary       ✅       ✅       🔄 (in)   —        —
prod-rest         ✅       ✅       —        —        —
```

合法狀態：**staging 在 Phase 4 + prod 在 Phase 2 同時發生**。playbook 不要假設「全 cluster 同步」。

---

## 10. Gate Reference Table

| Gate | Phase 出 | Phase 入 | 通過條件 |
|---|---|---|---|
| Gate 1 | Phase 0 Discovery | Phase 1 Pre-flight | Tier A 靜態 audit hard checks pass + migration-state.json committed |
| Gate 2 | Phase 1 Pre-flight | Phase 2 Shadow | Dual-write ≥ 7 天無掉點 + Tier B 比對 staging vs prod 無 cardinality drift |
| Gate 3 | Phase 2 Shadow | Phase 3 Cutover | **Subset overlap = 100%** + 新增 alert 顯式 sign-off + ≥ 2 週 shadow 期 |
| Gate 4 | Phase 3 Canary | Phase 3 全量 | Canary tenants 跨 24h 無 unexpected alert + ops sign-off |
| Gate 5 | Phase 3 全量 | Phase 4 Decommission | 全量切換 ≥ 1 ops cycle 無 incident |

**所有 Gate 用 invariants**（subset overlap、cardinality drift bound 等），**不是**「告警量一致」這類 timing-sensitive 命題。

---

## 11. Rollback 三層可逆界線

| Layer | 可逆性 | Rollback 機制 | 預估時間 |
|---|---|---|---|
| **Config**（rules.yaml / AM routing / `_defaults.yaml`）| ✅ | `git revert <commit>` → AM/exporter reload | < 5 分鐘 |
| **監控狀態**（已 silenced alert / maintenance window） | ⚠️ 半可逆 | git revert + manual cleanup script（待 ship） | ~30 分鐘 |
| **資料層**（VM 已 ingest 的 metric / Prom 已 GC 的 chunk） | ❌ 不可逆 | 接受 | — |

**playbook 必須讓客戶建立 mental model**：rollback ≠ undo all.

---

## 12. Failure Mode Catalog（cross-phase summary）

每 Phase 列已知 failure mode + hyper-realistic anchor。

> **`(e.g., ...)` 是 educated guess** — 不一定對應真實 incident #，但是基於業界 SRE 知識與本平台架構推斷的高機率事故。Maintainer review 時遇到團隊踩過的可順手補真 Issue #；沒踩過的保留作 defensive reminder（仍有 mental-anchor 價值）。深入排查 → `docs/integration/troubleshooting-checklist.md`（I-4 待 ship）。

### Phase 0 — Discovery & Inventory

| 症狀 | 第一手排查 | Anchor |
|---|---|---|
| **Tier A 卡在 PromQL syntax error** | `da-parser --strict-promql --report` 看哪些檔案 fail；常見是手寫 PromQL 用了 vmalert-only 函數但 source 標 prometheus | (e.g., 客戶混用 `histogram_quantile_bucket` (metricsql) 與 `histogram_quantile` (promql)，da-parser dialect detector 標 ambiguous) |
| **Tier A 撈到 100+ orphan rules** | 客戶聲稱「那些是 silenced」；驗證 AM silencer 是否仍 active；Tier B snapshot 比對 `silences[?] expires` | (e.g., 5 年前 silenced 一個 region 的 alert，silence 早 expire 但 rule 沒 prune → orphan 結果 = false positive) |
| **Tier A 抓到 hardcoded tenant id** | dev-rule #2 違反；migration-state.json 列出每處；Phase 1 之前必須 fix | (e.g., 急救 hotfix 留下 `instance="db-prod-1"` PromQL，原作者離職、rationale 失傳) |
| **PromRule CRD + 原始 rules.yaml 雙寫** | Operator 遷移過程留遺跡；da-parser dedupe 失敗時手動 reconcile | (e.g., 三年前 Operator 遷移半完成，PromRule 與 ConfigMap 並存，當前 active source 不明) |
| **Tier B `ALERTS{}` 查詢 timeout** | Prom 5+ 年沒 GC 或 cardinality 過高；改縮窗口 `ALERTS{}[1d]` 或接受 Tier B 缺失 | (e.g., 100k+ ALERTS series 全量查 30s timeout，改近 24h 約 2k series 可查) |
| **Tier C 來源不齊全（multi-region 各用不同 logging stack）** | 部分 region 用 ELK 部分用 Splunk → Tier C partial | (e.g., us-east 有 ELK 5y retention、eu-west 沒 → Tier C 只覆蓋 50% scope；接受並記入 migration-state.json `tier_c.coverage`) |
| **Tenant id naming collision** | 客戶 tenant 命名碰我們 reserved scheme（`prod` / `staging` / `default`）→ Phase 1 之前 rename | (e.g., 客戶用 `default` 作 fallback tenant id，與我們 routing default 衝突；da-tools onboard 建議改名 `customer-default`) |
| **Receiver 已死但客戶不知道** | 過期的 PagerDuty token、解散的 Slack channel、離職員工 email → Tier B 顯示「N 個 routes 從未 fire」 | (e.g., 客戶 ops 看到 dead receiver 列表反應「啊那個是 6 個月前的事故 owner，他離職了」；非 Phase 0 預期成果但常見） |

### Phase 1 — Pre-flight & Dual-Write

| 症狀 | 第一手排查 | Anchor |
|---|---|---|
| **vmagent OOMKilled in 初次 dual-write** | Pod restart count 飆升、events 含 OOMKilled；bump memory limit | (e.g., vmagent 初次 dual-write 用預設 64Mi limit、100k+ series + label cardinality bursts 直接 OOMKilled。bump 到 1Gi + reduce `-remoteWrite.maxBlockSize` 後穩定) |
| **VM disk 撐爆（dual-write 加倍 ingest）** | `vm_data_size_bytes` 增速超估算；client 估錯 cardinality | (e.g., 客戶估 10k tenant labels 但實際因 multi-region label combination 達 100k，VM single-node 24h 內 disk full；緊急上 hourly snapshot 撤 retention 或加 disk) |
| **exporter scrape timeout（conf.d 過大）** | exporter `/metrics` 30s timeout；conf.d 含 1000+ tenant 配置 | (e.g., conf.d 1500 tenants × 3 metrics each = 4500 series，single-shot serialize 慢；改 incremental rebuild 或 split conf.d 跨 shard) |
| **ServiceMonitor mismatch staging/prod** | exporter 在 prod 沒被 scrape；staging 用 Operator + ServiceMonitor / prod 還在 ConfigMap | (e.g., 多 cluster 不對齊部署模式，prod cluster 用 `kubernetes_sd_config` static target 而不是 ServiceMonitor，prod exporter pod 起來但無人 scrape) |
| **dual-write metric drift > 5%（Gate 1 fail）** | Prom relabel 與 vmagent relabel 不同步；diff 對應 metric 名 | (e.g., 客戶 Prom 有 `__tmp_metric_name` 拋棄 staging-only metrics 的 relabel rule，vmagent 沒抄；VM 比 Prom 多 5-8% metric → drift fail) |
| **firewall block exporter → VM remote_write** | exporter cluster 與 VM cluster 跨 region；NetworkPolicy / VPC peering 沒設 | (e.g., exporter 在 monitoring NS、VM 在 vm NS，NetworkPolicy egress 沒開 8480 port → exporter remoteWrite retry log 持續 connection refused) |
| **vmagent `pending_data_bytes` 長期 > 0** | 寫不完 → 警告 OOM 預兆；磁碟 buffer 累積 | (e.g., remote_write target 響應慢，vmagent buffer 從 0 漲到 500MB 後 hit memory limit；事故前一天 buffer 已開始累積但無 alert) |
| **threshold-exporter `user_threshold` 在 VM 查不到** | 確認 vmagent 有 scrape exporter；確認 VM ingest 沒 drop | (e.g., 客戶忘了把 exporter 加進 vmagent scrape config，metric 起飛但無人收集；driver 跑了 1 週才注意到 dashboard 是空的) |

> Phase 2-4 的 catalog 在後續 PR 補完（這份 PR 涵蓋 Phase 0 + Phase 1）。深入排查 connect 既定 [troubleshooting-checklist](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/377) 待 ship。

---

## 13. Appendices

### A. Customer-anon scenario walkthrough（待寫，~1.5 頁）

**Setting**：1000 tenant 製造業客戶，原本自管 Prom + AM 5 年，無 telemetry pipeline。Stage 4 maturity（mature multi-system）。要換 VM + 加 metric-split。

[walkthrough 帶讀者過完 Phase 0-4，每 phase 1 段]

### B. Cross-references

- **Schema**：[`docs/schemas/migration-state.md`](../schemas/migration-state.md) — `.da/migration-state.json` 欄位 spec
- **Shadow 機制深入**：[`docs/shadow-monitoring-sop.md`](../shadow-monitoring-sop.md)
- **Rule-only migration**（1/2-system）：[`docs/migration-guide.md`](../migration-guide.md)
- **Staged adoption**（custom_ → golden 漸進）：[`docs/scenarios/staged-adoption-guide.md`](staged-adoption-guide.md) — I-2，已 ship
- **Troubleshooting**：`docs/integration/troubleshooting-checklist.md` — I-4，待 ship
- **VM integration entry**：[`docs/integration/victoriametrics-integration.md`](../integration/victoriametrics-integration.md) — I-3，已 ship

### C. ADR / Design references

- 設計 commitments lock from PR #375 strategic discussion + 3 輪 Gemini adversarial review
- 5-Phase / Gate invariants / Plan A vs B / Rollback 邊界 / X-Y matrix 全 locked
- 內文寫作會在後續 PR 補進每 Phase 的 narrative + checklist 詳細

---

## Outline Status

| 段 | 狀態 |
|---|---|
| §0-2 frame + decision tree + 5-Phase overview | ✅ outline ready |
| §3-7 各 Phase 30-sec TL;DR + checklist 骨架 | ✅ outline ready |
| §3 Phase 0 Architect Narrative | ✅ 內文 ship（本 PR） |
| §4 Phase 1 Architect Narrative | ✅ 內文 ship（本 PR） |
| §5-7 Phase 2/3/4 Architect Narrative | 🟡 待補（PR-2 / PR-3）|
| §12 Failure Mode Catalog Phase 0+1 | ✅ 內文 ship（本 PR；用 hyper-realistic mock anchors） |
| §12 Failure Mode Catalog Phase 2/3/4 | 🟡 待補（PR-2 / PR-3） |
| §13 Customer-anon walkthrough | 🟡 待補（PR-3，composite Frankenstein 寫法） |
| §8 Plan A vs B Git layout | ✅ outline ready |
| §9 X-Y matrix | ✅ outline ready |
| §10 Gate Reference Table | ✅ outline ready |
| §11 Rollback 三層 | ✅ outline ready |
| §12 Failure Mode Catalog | 🟡 待補 |
| §13 Customer-anon walkthrough | 🟡 待補 |
| §13 Cross-refs | ✅ outline ready |

**下一步**：本 outline 進 PR review（owner + Gemini）→ 通過後動內文 PR（補 Architect Narrative 段 + Failure Mode Catalog + Walkthrough）。預計內文 ~8-12h，分 1-2 PR ship。
