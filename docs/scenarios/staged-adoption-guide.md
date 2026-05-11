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

1. **Shadow 期數據佐證**：shadow monitoring 期間（multi-system migration playbook §5）至少 2 週
2. **Coverage gate（取代純 100% overlap）**：以下二擇一**並滿足對應子條件**：
   - **(2a) Subset overlap = 100%**：custom_ 觸發的條件 golden 必觸發（避免 catastrophic 假陰性）— 適用「golden 是 custom_ 的等價或超集」情境
   - **(2b) Intentional noise reduction**：custom_ 觸發但 golden 沒觸發的 case **每筆**滿足：
     - domain owner 顯式分類為「intended noise filter」（不是 bug）
     - reviewer 能 articulate 為什麼 golden 的 smarter logic（多 condition / time-window / threshold tuning）正確抑制了該 alert
     - 對應的「為什麼這次沒叫」推理寫進 PR description（給未來 incident review 翻查）
   - 嚴禁「reduction 因為 golden 漏抓 catastrophic case」混進 (2b)；若有疑慮 default 走 (2a)
3. **多出的 alert 顯式 sign-off**：golden 比 custom_ 多觸發的 alert 不視為 bug、客戶確認是 design intent

> **設計動機**（對抗 "100% overlap 悖論"）：直接寫死 100% overlap 會困住客戶在爛規則裡。範例：客戶舊 `custom_mysql_cpu` 是 `cpu > 80%`（一天叫 50 次），新 golden 是 `cpu > 80% AND io_wait > 20% for 5m`（一天叫 5 次該叫的）。Shadow 期 overlap 必然不到 100% — 但這是 **intended noise reduction**，不是 regression。Gate (2b) 給這條合理路徑。

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
- [ ] **Coverage gate** 二擇一：
  - [ ] (2a) Subset overlap = 100%（`da-tools shadow-verify --check-subset-overlap`）— 預設嚴格路徑
  - [ ] (2b) Intentional noise reduction — overlap 不足 100% 時，每筆 missing case 在 PR description 含 domain owner 「為什麼這次沒叫」分類 + reviewer 推理
- [ ] 多出 alert 列表已給 customer ops sign-off（PR description 含 sign-off 紀錄）
- [ ] Customer ops 已熟悉 `git revert <batch-commit>` rollback path
- [ ] Domain owner approval（Slack / email / PR review approval）
- [ ] Rollback 演練紀錄保留 30 天
</details>

---

## 5. Batch sizing — 一次推多少

### 30-sec TL;DR
- **預設 = 1 domain × 1 region × canary tenant-group（5% tenants）**
- 第一波只切 canary tenants 確認，再展開到該 region 全 tenant
- 1000 tenant 客戶估 ~10 batches × (canary + full) × 1 ops cycle = ~10-12 週
- 跑得快 = 風險高，沒救命的東西就慢慢推

### 決策原則

| Batch 粒度 | 規則 × tenant 數 | 適用 | 風險 |
|---|---|---|---|
| **單規則 × canary tenants** | 1 × 5%-tenants | 高風險 / 客戶第一次接觸 | 最低風險，但太慢 |
| **1 domain × 1 region × canary tenants**（**預設**）| 5-15 × 5%-tenants | 第一波 | 最佳平衡 |
| **1 domain × 1 region × full tenants** | 5-15 × 100%-tenants | canary 跨 1 ops cycle 通過後 | 中等 |
| **1 domain × 全 region × full tenants** | 20-60 × 100% | domain 已成熟 / 三批以上無事故 | 較高，跨 region 變因擴大 |
| **跨 domain bundle × full tenants** | 50+ × 100% | 急著收尾、客戶已熟 | blast radius 大、不建議 |

**為什麼預設加 canary tenant 維度**：

- Multi-tenant 平台架構下，「1 domain × 1 region」已經是 1000 個 tenant 全切 — blast radius 太大
- 先切 5% canary tenants（建議挑容忍度高的內部 / 早期合作客戶）→ 跨 1 ops cycle 觀察 → 確認沒問題 → 再展全 region tenant
- 客戶通常會指定一些「dev / staging tenants」或「先行體驗組」當 canary 池
- Domain 隔離 + Region 隔離 + Tenant 隔離 = **三層 blast radius 圍欄**

### Canary tenant 選擇原則

- **優先**：客戶內部 tenant、staging-only tenant、容忍度高的早期客戶
- **避免**：production-critical tenant、客戶 SLA tier 最高的、客戶剛拉警報的
- **數量**：5-10% 是甜蜜點；少於 5% 訊號太弱、多於 10% blast 範圍開始有意義

### 加速的條件

- 第一批 canary 跨 1 ops cycle 完美 → 推 full tenant 級
- 連續 3 個 batch（canary + full）無事故 → 可加大到 1 domain × 全 region × full
- 但**永遠不要**跳過 canary 階段，加速是規模、不是步驟

<details>
<summary>📋 Batch planning checklist</summary>

- [ ] Inventory：列當前所有 `custom_*` 規則 + 對應可 promote 的 golden 規則
- [ ] 分群：按 domain × region 切分
- [ ] 排序：低風險 / 多訊號的 domain 先（DB / network 通常先；business KPI 後）
- [ ] **指定 canary tenant pool**：與客戶協商 5-10% 容忍度高的 tenants（內部 / staging / 早期合作）
- [ ] 第 1 批：最小粒度（5-10 規則）× canary tenant pool — 建立流程信心
- [ ] PR description 含 mapping table（custom_ name → golden name）+ canary tenant 清單
- [ ] Canary 跨 1 ops cycle 後再 PR full-tenant promotion
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

**關鍵決策**（**這是 domain owner 業務判斷，不打算自動化**——見下方）：
- 哪些規則有 golden 等價物：domain owner 對照 [Rule Pack ALERT-REFERENCE](../rule-packs/ALERT-REFERENCE.md) 手動比對 custom_ 與 golden 規則的觸發條件 + 語意
- 哪些規則該留 custom_（無 Rule Pack 對應、客戶業務專屬）

> 📝 **為什麼不做 `rule-pack-mapping --suggest` 工具**（追蹤：[issue #405](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/405)）：自動建議「custom_X 應 promote 到 golden Y」需要深度 PromQL 語意分析（AST + 等價判斷），準確率天花板低；建議錯了反而給客戶 false confidence。這類**業務語意判斷**保留為人類決策，不勉強自動化。`rule-pack-diff`（兩版本之間的機械差異）是 factual 工作，會做；`--suggest`（判斷哪條 custom 該 promote）是 judgment 工作，不會做。

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

#### ⚠️ Disablement drift — 雙重告警災難的真實風險

當客戶寫了 `custom_*` 規則，**通常意味著他們也在系統內 disable / silenced 了對應的 v1 golden 規則**（避免 custom_ 與 golden 同時叫造成 double-firing）。

Rule Pack v2 升級時若**改了 alert name 或 label schema**，客戶針對 v1 的 disable 配置（例：`_defaults.yaml` 的 `disable: [<v1-name>]` 或 AM silencer 的 `matchers: [alertname="<v1-name>"]`）**可能 silently 失效**。後果：

1. 客戶 `custom_*` 仍照常叫
2. v2 golden（disable 沒命中）也跟著叫
3. → **alert storm**（同 incident 兩條 alert paths 同時 page，PagerDuty 狂響）

**SOP**：

1. 列 Rule Pack v1→v2 差異點（含 alertname / label schema breaking changes）
   - **⚠️ `da-tools rule-pack-diff --from=v1 --to=v2` 尚未 ship**（追蹤：[issue #405](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/405)）
   - **手動 workaround**：對照 Rule Pack 該版本的 `CHANGELOG.md`（[ADR-REFERENCE](../rule-packs/ALERT-REFERENCE.md) 列當前 stable alertname）+ `git diff rule-packs/<pack>/v1.0.0/...rule-packs/<pack>/v2.0.0/`
2. **Disablement drift check**：對每個客戶有 `custom_*` 的 alert，驗證對應的 disable 配置是否仍命中 v2：
   - `_defaults.yaml` 的 disable list — 確認 v2 alertname 也在清單上（或加進去）
   - AM 的 silencer matchers — 確認 v2 label schema 不會讓既有 matcher mismatch
   - **缺哪一個就會 double-fire；補上才能 ship v2**
   - 具體排查命令見 [troubleshooting-checklist §1.3.2](../integration/troubleshooting-checklist.md#132-silencer-mismatchdisablement-drift-double-fire-alert-storm)
3. 對每個 `custom_*`：判斷 v2 是否吸收 → 若是，本 guide 的 promotion 流程跑
4. 對 v2 改語意的：`custom_*` 重寫對齊 v2 schema 或留原樣（**且** disable 配置同步更新）
5. 對 v2 breaking：必須要 promote（被動 forced upgrade）

**Audit hook 建議**（追蹤 [issue #405](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/405)）：`da-tools upgrade-check` 跑 v1→v2 升級時自動偵測 disablement drift，列「會 double-fire 的 alert」清單，merge 前須清零。可能與 `silencer-drift-check` 合併實作。

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
- **Rule Pack 速查**：[`rule-packs/ALERT-REFERENCE.md`](../rule-packs/ALERT-REFERENCE.md)
- **設計依據**：本文件框架由 PR #389 strategic discussion 結晶化，鎖定為「lifecycle pattern not migration step」觀點（取代「migration final step」的初版設計）

