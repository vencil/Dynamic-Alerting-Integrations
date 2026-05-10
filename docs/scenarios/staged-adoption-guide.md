---
title: "Staged Rule Adoption Lifecycle"
tags: [scenarios, lifecycle, custom-rules, golden-rules, rule-packs]
audience: [platform-engineers, sre, tenant-admins]
version: v2.7.0
lang: zh
---

# Staged Rule Adoption Lifecycle

> **這不是「遷移最後一步」**，是貫穿客戶整個生命週期的 **rule curation pattern**：每次有新客製規則進入時、每次平台 ship 新 Rule Pack 時、每次新團隊接管 tenant 時都適用。
>
> **適用三大情境**（§7 詳述）：
> 1. **Initial migration** — 大量 `custom_*` 規則初次導入後漸進收編
> 2. **New tenant onboarding** — 半年後新團隊上線，同樣走「先 custom_、後 golden」路徑
> 3. **Rule Pack upgrade** — 平台 ship 新 golden 規則時，既有 `custom_*` overrides 重新評估

---

## 1. 兩個狀態：`custom_*` vs golden

| 狀態 | Naming | 來源 | Trust 模型 |
|---|---|---|---|
| **`custom_*`** | `custom_<domain>_<metric>` | 客戶從既有系統手動 import / 自寫 | 客戶 own，平台不擔責 |
| **golden** | `<rule-pack-name>:<metric>` | 平台 curated Rule Pack | 平台 own，per-version SLA |

**核心動機**：客戶從異質系統來，**規則是業務邏輯**，不能 force-strip 重寫。`custom_*` 給客戶 buffer 帶既有邏輯進來；golden 是長期目標。

→ 詳細 namespace 規則見 [`custom-rule-governance.md`](../custom-rule-governance.md)。

---

## 2. 為什麼 staged，不是 big-bang

| 風險 | Big-bang | Staged |
|---|---|---|
| **規則漏抓** | 一次切失就 blast radius=全 tenant | 一次切失只影響當批 |
| **語意漂移** | golden 與 custom_ 行為微差被一起爆出來 | 每批驗證等價，差異點隔離 |
| **客戶信心** | 「全切了之後出事就 paged」恐懼 | 看到第一批穩了，下批才推 |
| **Rollback** | 整批 git revert 影響面大 | per-batch revert，blast radius 小 |
| **知識轉移** | 客戶沒時間理解 golden 語意 | 每批配一份 mapping 文件 |

**Best-practice from SRE 社群**：每次 production cutover 都應該有 *canary tier* + *observation period*，staged adoption 是這個原則在 rule layer 的應用。

---

## 3. 三層 reading speed

每節（§4–§6）含同樣結構：

- **30-sec TL;DR** — 經理跟主管溝通用
- **決策原則** — architect 看（為什麼這麼決定）
- **Operator Checklist**（`<details>` 折疊）— on-call 跑 promotion 用

---

## 4. When to promote — 決策準則

### 30-sec TL;DR
- **Hard 必要條件**：shadow phase 期 ≥ 2 週、subset overlap = 100%、無 unexpected new alert
- **客戶 sign-off**：domain owner 確認 golden 語意與既有 custom_ 等價
- **Rollback path 預演過**：git revert 命令客戶 ops 已熟練

### 決策原則

**Hard 必要條件**（缺一不可）：

1. **Shadow 期數據佐證**：shadow monitoring 期間（multi-system migration playbook §5）至少 2 週，golden 與 custom_ 在 subset 條件上 100% 共觸發
2. **Subset overlap = 100%**：custom_ 觸發的條件 golden 必觸發（**避免 catastrophic 假陰性**）
3. **多出的 alert 顯式 sign-off**：golden 比 custom_ 多觸發的 alert 不視為 bug、客戶確認是 design intent

**Soft 加分條件**（任一滿足、推薦推進）：

- Domain owner（DBA / SRE / platform team）對 golden 語意點頭
- 客戶有 incident response runbook 可對接 golden 的 alert label schema
- 有時間做 1 個 ops cycle 觀察（建議 1 週，含 weekly 異常）

### 例外：強制留 `custom_` 的情境

- 客戶有專屬的 metric source 不在 platform metric dictionary（例：自寫的 business-KPI scrape exporter）
- 客戶有 compliance 規定 alert text 必須含特定欄位（golden 不一定 cover）
- Domain 太小眾沒對應 Rule Pack（例：客製化 IoT device monitoring）

→ 留 `custom_` 不是失敗，是 **stable equilibrium**。本 guide 的 promotion 不強制全推到 golden。

<details>
<summary>📋 Promotion gate checklist（給 executor）</summary>

- [ ] Shadow 期 ≥ 2 週、無 alert noise（`da-tools shadow-verify --window=14d`）
- [ ] Subset overlap = 100%（`da-tools shadow-verify --check-subset-overlap`）
- [ ] 多出 alert 列表已給 customer ops sign-off（PR description 含 sign-off 紀錄）
- [ ] Customer ops 已熟悉 `git revert <batch-commit>` rollback path
- [ ] Domain owner approval（Slack / email / PR review approval）
- [ ] Rollback 演練紀錄保留 30 天
</details>

---

## 5. Batch sizing — 一次推多少

### 30-sec TL;DR
- **預設 = 1 domain × 1 region**（典型 5-15 條規則）
- 1000 tenant 客戶分 ~10 batches、每 batch 跨 1 ops cycle、總時程 ~10 週
- 跑得快 = 風險高，沒救命的東西就慢慢推

### 決策原則

| Batch 粒度 | 規則數 | 適用 | 風險 |
|---|---|---|---|
| **單規則** | 1 | 高風險 / 客戶第一次接觸 | 太慢，10000 規則需要太久 |
| **1 domain × 1 region** | 5-15 | **預設** | 平衡點 |
| **1 domain × 全 region** | 20-60 | domain 已成熟 / 第二批起 | 跨 region 變因擴大 |
| **跨 domain bundle** | 50+ | 急著收尾、客戶已熟悉 | blast radius 大、不建議 |

**為什麼 1 domain × 1 region 是預設**：

- Domain 隔離：MySQL alert 出問題不影響 Postgres alert
- Region 隔離：staging-us-east 出問題不影響 prod-eu-west
- 心智可管理：5-15 條規則，operator 一個工作日能跟完所有 alert

### 加速的條件

- 第一批跨 1 ops cycle 完美（無 unexpected alert）→ 第二批可考慮 1 domain × 全 region
- 連續 3 批無事故 → 可加大到跨 domain bundle
- 但**永遠不要**跳過 ops cycle 觀察期，加速是規模、不是時間

<details>
<summary>📋 Batch planning checklist</summary>

- [ ] Inventory：列當前所有 `custom_*` 規則 + 對應可 promote 的 golden 規則
- [ ] 分群：按 domain × region 切分
- [ ] 排序：低風險 / 多訊號的 domain 先（DB / network 通常先；business KPI 後）
- [ ] 第 1 批用最小粒度（5-10 規則）建立流程信心
- [ ] PR description 含 mapping table（custom_ name → golden name）
</details>

---

## 6. Observation period & rollback

### 30-sec TL;DR
- **每批至少 1 ops cycle**（建議 1 週，跨 weekly 異常窗口）
- Observation 期內出事 = `git revert` per-batch commit
- 監控狀態（已 silenced）需 manual cleanup（同 multi-system playbook §11 半可逆 layer）

### 觀察期長度建議

| 觀察期 | 適用 | 抓得到 |
|---|---|---|
| **24h** | 最少；canary tenant tier | smoke test、obvious regression |
| **1 ops cycle (typically 1 week)** | **預設** | weekly batch jobs / weekly cron alerts |
| **1 month** | high-stakes domain（compliance / financial） | monthly closing alerts、quarterly anomalies |

**抓不到的**：罕見事件（quarter-end / 大型 promo / black-friday traffic）— 需要事先 sync 客戶 calendar 避開敏感窗口。

### Rollback path

```bash
# 找出該 batch 的 commit
git log --grep="staged-adoption batch <N>" --oneline

# Revert
git revert <commit-sha>

# AM / exporter 自動 reload
# 監控狀態 cleanup：
#   - Silenced alerts → 手動 unsilence（AM UI）
#   - Maintenance windows → 手動 close（tenant API）
```

**Rollback 的可逆界線**（同 [multi-system-migration-playbook §11](multi-system-migration-playbook.md#11-rollback-三層可逆界線)）：

| Layer | 可逆性 |
|---|---|
| Config（custom_ rule 改回 golden）| ✅ git revert |
| 監控狀態（已 silenced alert）| ⚠️ manual cleanup |
| 資料層（已 ingest 的 metric）| ❌ 接受 |

<details>
<summary>📋 Observation + rollback checklist</summary>

**Observation phase**
- [ ] Batch promotion PR merge 之後計時 1 ops cycle
- [ ] 每天看 alert volume + receiver delivery（`da-tools alert-quality --tenant=<batch-tenants>`）
- [ ] 跑 smoke 確認新 alert label schema 對 receiver compatible
- [ ] 跨週末 / 夜班 / 月底等 corner case

**Rollback (if triggered)**
- [ ] 在 Slack / on-call channel announce
- [ ] `git revert <batch-commit>` + push to GitOps branch
- [ ] AM reload (auto via ArgoCD / Flux)
- [ ] Manual unsilence affected alerts
- [ ] Postmortem：哪條規則出事、golden 與 custom_ 在哪個輸入差異
- [ ] 修 golden 或留 custom_，不要假裝不是 bug
</details>

---

## 7. 三大情境（lifecycle 框架）

### 7.1 Initial migration（首次導入）

**觸發**：multi-system migration playbook 走完 Phase 3 全量 cutover（所有 custom_ 都 active），現在開始 promote 到 golden。

**關鍵決策**：
- 哪些規則有 golden 等價物（`da-tools rule-pack-mapping --suggest`）
- 哪些規則該留 custom_（無 Rule Pack 對應、客戶業務專屬）

**結束條件**：所有 promote-able 都升級完，或客戶決定停在某個點。

### 7.2 New tenant onboarding（半年後新團隊）

**觸發**：平台已 stable，新團隊接管某個 service / 引進新 tenant。

**模式**：
- 新團隊先用 Rule Pack（golden）作 starter
- 對 Rule Pack 不滿意的條件先寫 `custom_*` 覆蓋
- 等 Rule Pack 自然演化匹配新團隊需求 → promote `custom_*` → 刪 override

**這跟 7.1 反過來**：7.1 是 custom_ → golden，7.2 是 golden → custom_ → golden（反向 onboarding）。但**判斷條件相同**（subset overlap、observation period、rollback path）。

### 7.3 Rule Pack upgrade（平台 ship 新版）

**觸發**：平台 ship Rule Pack v2，客戶有既存的 `custom_*` 是針對 Rule Pack v1 的 override。

**問題**：Rule Pack v2 可能：
- 加新 alert（客戶 `custom_*` 不需要 override）
- 改 alert 語意（客戶 `custom_*` override 過時、變成 redundant）
- breaking 既有 label（客戶 `custom_*` 還用舊 label 名）

**SOP**：
1. 跑 `da-tools rule-pack-diff --from=v1 --to=v2` 列差異點
2. 對每個 `custom_*`：判斷 v2 是否吸收 → 若是，本 guide 的 promotion 流程跑
3. 對 v2 改語意的：`custom_*` 重寫對齊 v2 schema 或留原樣
4. 對 v2 breaking：必須要 promote（被動 forced upgrade）

### 三情境的共同模式

不論哪一種，promotion 都走 §4-§6 同樣的決策準則 / batch sizing / observation period — 這是 lifecycle pattern 而非 migration step 的核心理由。

---

## 8. 與其他 doc 的關係

```
multi-system-migration-playbook.md  ─ Phase 3 cutover (routing flip 完)
                       │
                       └─ Phase 4 starts ──────────►
                                                  │
                                                  ▼
                              本 guide (Staged Adoption Lifecycle)
                                                  │
                                                  ├─► custom-rule-governance.md (custom_ namespace 規則)
                                                  ├─► multi-system-migration-playbook §11 (Rollback 界線)
                                                  └─► rule-packs/ALERT-REFERENCE.md (golden 速查)
```

- **multi-system-migration-playbook**：負責 cutover routing（migration 期一次性事件）
- **本 guide**：負責 promotion lifecycle（每次新規則入境都適用）
- **custom-rule-governance**：定義 `custom_*` namespace 規則本身
- **rule-packs/ALERT-REFERENCE**：列 golden alert 全表、客戶查 mapping 用

---

## 9. 何時 *不要* 用 staged adoption

- **規則只有 1-2 條**：直接 promote，staged 反而 ceremony
- **dev / staging 環境**：可以全推、出事重來、學習快
- **單純的 noise reduction**：把太吵的 alert 關掉不需要 staged

**核心判斷**：staged 的 cost 是時間 + ops attention；只在 blast radius * cutover frequency 夠大時才值得。

---

## 10. 觀察 metric & dashboard

平台側可看：

- `da_alert_promotion_batch_status{batch_id, status="active|reverted|done"}`
- `da_alert_quality_diff{custom_, golden, metric}`（若 enabled）
- Grafana dashboard：`Staged Adoption Progress` panel（v2.9 ship；目前用 [grafana-dashboards](../grafana-dashboards.md) 既有 shadow-rules-active panel 觀察）

→ Phase 3 期 + 1 個 ops cycle 內持續看；之後 dashboard 可降級到 weekly review。

---

## 11. Cross-references

- **規則 namespace governance**：[`docs/custom-rule-governance.md`](../custom-rule-governance.md)
- **Multi-system migration（cutover 期）**：[`docs/scenarios/multi-system-migration-playbook.md`](multi-system-migration-playbook.md)
- **Shadow monitoring SOP**：[`docs/shadow-monitoring-sop.md`](../shadow-monitoring-sop.md)
- **Rule Pack 速查**：[`rule-packs/ALERT-REFERENCE.md`](../../rule-packs/ALERT-REFERENCE.md)
- **設計依據**：本文件框架由 PR #389 strategic discussion 結晶化，採 Gemini 「lifecycle pattern not migration step」觀點

---

## 12. Outline status

| 段 | 狀態 |
|---|---|
| §1-3 兩狀態 + staged 動機 + reading speed | ✅ first ship |
| §4 Promotion 決策準則 + checklist | ✅ first ship |
| §5 Batch sizing | ✅ first ship |
| §6 Observation + rollback | ✅ first ship |
| §7 三大情境 lifecycle 框架 | ✅ first ship |
| §8-11 與其他 doc 關係、不適用情境、cross-refs | ✅ first ship |
| §10 dashboard / metric 細節 | 🟡 部分（dashboard 待 v2.9 ship） |
| Concrete customer walkthrough | 🟡 待補（後續 PR 加 1 個 representative example） |

EN mirror 暫不 ship（同 [multi-system-migration-playbook](multi-system-migration-playbook.md)），等 outline review 通過後一起補。
