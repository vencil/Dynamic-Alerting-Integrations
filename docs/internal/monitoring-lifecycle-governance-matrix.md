---
title: "Monitoring Lifecycle × Role × Governance-Gate — SSOT 矩陣"
tags: [internal, governance, lifecycle, adoption]
audience: [platform-engineers, sre]
lang: zh
---

# Monitoring Lifecycle × Role × Governance-Gate — SSOT 矩陣

> **用途**：讓「宣告式自動化能減輕負擔」從 overclaim 變成真的**前提模型**。所有下游產物（golden path、CRD schema、enforced gate、per-role quickstart）都**從這張表 derive**。一個行為若不在這張表裡，它就不該有 CRD 欄位、也不該有 gate。
> **由來**：源自 [ADR-026](../adr/026-node-maintenance-liveness-suppression.md) 探索期的 Gate 1 實證 + 角色/工具/gate 盤點。對應長期 standing goal：**降低採用認知門檻**。
> **怎麼讀**：每列是一個 {角色 × 生命週期階段} 的動作。看 `Disp` 欄判斷自動化程度（🟢/🟡/🔴，見圖例），看 `Reuse/Gap` 欄找待建項（`Gap` = net-new）。生/老多為 reuse，痛點集中在病/死。

## 設計原則（唯一該存在的「方法論」文件）

1. **自動化安全度 ∝ 1 / (該生命週期階段的 blast-radius × 判斷量)**。生/老 → 自動化；病/死 → 引導+閘門，**絕不無腦**。
2. **治理用 enforced gate 交付，不用散文**。人不該為了合規去讀政策——閘門擋住違規。專案**已是此 pattern**：secret-scan、iac-lint、tenant-api admission validators、`policy_opa_bridge`。
3. **指標是 per-person load（角色 R 安全做階段 L 要讀多少），非文件總頁數**。總頁數可能上升，但每人負擔靠四招崩塌：AUTO（無文件）+ GUIDED（查狀態不讀手冊）+ HARD-GATE（強制不警告）+ audience 切片。
4. **能活下來的文件只有三種**：(a) 這張矩陣；(b) 自動 emit 的 reference（`kubectl explain` / `--help` / CRD status）；(c) 每角色一份 quickstart。其餘都是 smell。
5. **前提**：自動化只在 model 利落時砍文件；糊 model 上蓋 CRD **反而加**文件 → 故這張矩陣（利落 model）才是真正第一交付，不是 CRD/cron。

## Disposition 圖例

- 🟢 **AUTO** — 系統替你做，~零文件，低 blast-radius
- 🟡 **GUIDED** — 系統持有狀態 + 建議下一步（查不讀），人確認
- 🔴 **HARD-GATE** — 高 blast-radius，系統強制護欄 + 要顯式核可/audit，**永不無腦**

## 角色（取會「動作」的 5 個；decision-maker 唯讀評估、不入動作矩陣）

| 角色 | 介面 | 一句定義 |
|---|---|---|
| **Platform Engineer (PE)** | GitOps / Helm / CLI | 操作平台本體、全域 defaults/routing/rule-pack |
| **Tenant Admin (TA)** | tenant-api / portal | 自助管自己 tenant 的 threshold/routing/maintenance |
| **SRE / On-call** | 唯讀 dashboard + ops CLI | 事故反應、監控健康、遷移/cutover |
| **Domain Expert (DE)** | rule-pack YAML | 擁有某 domain 的 rule-pack 內容 |
| **Security / Compliance (SEC)** | audit / RBAC / OPA | 稽核、多租戶隔離、政策邊界 |

## 矩陣

### 生 PROVISION（建置/上線）— 多數 🟢，重度 gated（reuse 為主）

| 角色 | 動作 | 工具/機制 | Gate（已存在） | Disp | Reuse/Gap |
|---|---|---|---|---|---|
| PE | bootstrap 平台 | `init_project` · `operator_generate` · Helm · `federation_keygen`/`federation_check` | 數十條 pre-commit + CI required（iac-lint、secret-scan、rulepack-drift、single-writer-invariant） | 🟢 | reuse；缺**一個宣告式入口**（ArgoCD ApplicationSet bundle） |
| TA | 上線自己 tenant | `scaffold_tenant` · tenant-api PUT · portal wizard | `body_validator` · `ValidateTenantCustomAlerts` · `CheckTenantRootKeys` | 🟢🟡 | reuse |
| SRE | 整合進既有 stack / cutover | `onboard_platform` · `migrate_rule` · `validate_migration` · `cutover_tenant` · `shadow_verify` | promtool · `blast_radius` | 🟡 | reuse（判斷重→GUIDED）；缺打包成 golden path |
| DE | 寫 rule-pack | `rule-packs/` YAML · `lint_custom_rules` | promtool unit · pint · deny-list/required-labels · `hardcode-tenant-check` | 🟢 | reuse |
| SEC | 上線時驗隔離 | RBAC · federation admission（label-enrichment）· CODEOWNERS | admission validators | 🔴 | reuse |

### 老 OPERATE（日常運行/調參）— 🟢🟡，admission + runtime guard

| 角色 | 動作 | 工具/機制 | Gate（已存在） | Disp | Reuse/Gap |
|---|---|---|---|---|---|
| PE | 調 defaults/routing | `patch_config` · `generate_alertmanager_routes` · `explain_route` · `config_diff` | `guard/routing.go`（5 檢查）· `blast_radius` PR 留言 · GitOps PR | 🟡 | reuse |
| TA | 調自己 threshold/routing | tenant-api PUT · portal | admission validators · SHA-256 hot-reload | 🟢🟡 | reuse |
| SRE | 健康/診斷/品質 | `diagnose` · `batch_diagnose` · `alert_quality` · `alert_correlate` · `runtime_audit` · `check_alert` | （唯讀為主） | 🟡 | reuse |
| DE | threshold 治理 | `threshold_recommend` · `backtest_threshold` · **`threshold_govern` CronJob**（recommend→gate→PR） | `ha-max-threshold-aggregation` · `guard/cardinality.go` | 🟢 | reuse（自動調參迴路**典範**，他階段可仿） |
| SEC | 稽核變更 | `config_history` · GitOps commit trail · `policy_engine`/`policy_opa_bridge` | OPA bridge | 🟡 | reuse |

### 病 MAINTAIN/DEGRADE（維護/drain/事故）— ⚠️ THIN，gate 多為 SOFT（[ADR-026](../adr/026-node-maintenance-liveness-suppression.md) 所在）

| 角色 | 動作 | 工具/機制 | Gate（已存在） | Disp | Reuse/Gap |
|---|---|---|---|---|---|
| PE | cluster/node 維護 | `maintenance_scheduler` CronJob · `silencer_drift_check` | silence `endsAt` dead-man's-switch · drift（advisory） | 🟡 | **窄 GAP（ADR-026）**：平台 liveness 類的 maintenance-aware 抑制；cordon-aware 為 defer |
| TA | tenant 維護窗 | `_state_maintenance`（PromQL opt-out via `user_state_filter`）+ recurring → `maintenance_scheduler` | `unless on(tenant)` 抑制 | 🟡 | reuse |
| SRE | 事故反應 | `blind_spot_discovery` · `alert_correlate` | — | 🟡 | **GAP：無事故 runbook codify、無 degraded-state workflow** |
| SEC | 確保抑制有界/可稽核 | `silencer_drift_check`（**advisory only**） | （無 hard gate） | 🔴 | **GAP：silencing 無 hard gate / 無 audit trail** |

### 死 RETIRE（退役/刪 monitoring）— ⚠️ 最稀 + **0 條 HARD gate**

| 角色 | 動作 | 工具/機制 | Gate（已存在） | Disp | Reuse/Gap |
|---|---|---|---|---|---|
| PE | 退役 cluster/federation | `offboard_tenant`（preflight）· `state_reconcile` | （無 cascading gate） | 🔴 | **GAP：無級聯 teardown / orphan 清理；退役時序引爆 liveness（[ADR-026](../adr/026-node-maintenance-liveness-suppression.md)）** |
| TA | offboard 自己 tenant | `offboard_tenant` | （手動 git commit） | 🔴 | **GAP：手動** |
| SRE | 刪規則 / 清 silence | `deprecate_rule` · `silencer_drift_check`（advisory） | （無強制清理） | 🔴 | **GAP：orphan silence 清理非 enforced** |
| DE | 棄用 rule-pack metric | `deprecate_rule` | — | 🟡 | reuse |
| SEC | 稽核刪除（最高 blast-radius） | `federation/orphan/detector.go`（**僅 federation token**） | （無通用退役 audit/核可閘） | 🔴 | **GAP：刪 monitoring = 製造盲區，目前 ungated** |

## Gap Register（可執行輸出：net-new vs reuse，按優先）

1. **病 — 平台 liveness 類 maintenance-aware 抑制**：見 [ADR-026](../adr/026-node-maintenance-liveness-suppression.md)（窄 PARTIAL、`tenant_metadata_info` anti-join、HA-exporter-first）。cordon-aware 為 defer。
2. **死 — 退役安全（最高 blast-radius，目前 0 hard gate）**：級聯 teardown + orphan 清理編排 + **一條 HARD pre-merge gate** + 「移除/靜音 monitoring」audit。重用 `blast_radius` + `silencer_drift_check` + `orphan/detector`，但**升級成 enforcing 而非 advisory**。**具體首個失敗模式（[ADR-026](../adr/026-node-maintenance-liveness-suppression.md) 外審揪出）**：退役時若先刪 exporter 部署、conf.d 還在 → `up` 斷但 `tenant_metadata_info` 仍發 → liveness 規則對退役租戶噴 critical。⇒ 第一條 hard-gate 即「**禁止 PR 只刪 K8s target 卻殘留 conf.d**」（強制 conf.d 先移或兩者同移，先切斷 metadata 源）。
3. **治理 audit 缺口**：silencing / `threshold:"disable"` / `_silent_mode` 的 delta 無 hard gate / 無 audit → 升進 CI-blocking，接 `policy_opa_bridge`。
4. **跨切：系統持有「lifecycle 狀態」**（doc 崩塌的核心引擎）：per-tenant/per-cluster 生命週期狀態面，讓角色**查「我在哪階段 / 下一步合法動作」**而非讀文件。可騎 tenant-api `configwatcher` + customalerts `status` pattern。

## 這張矩陣如何崩塌文件量

- 🟢 cell → 無 how-to（工具做掉）；reference 自動 emit。
- 🟡 cell → 系統顯示狀態 + 下一步（查不讀）；每角色一份 quickstart。
- 🔴 cell → 閘門強制（違反不了→不必讀政策）+ audit；文件縮成「閘門存在 + 怎麼申請核可」。
- **淨效**：explanation/方法論矩陣 → 崩塌成**這一張表 + 自動 reference + 5 份 quickstart**。總頁數可能不降，但**每人要讀的崩塌**——那才是「減輕負擔」。

## 兩個 STOP-gate（決定要不要做，非怎麼做）

- **Gate 1（病）**：乾淨 rolling drain 下**實測**還有哪些 alert 真的響？→ 已由 ADR-026 靜態收斂為 narrow PARTIAL。
- **Gate 2（CRD-as-API）**：有**真實客戶**活在 kubectl 裡？有 → CRD `explain`/`status` UX 才值得；沒有 → golden-path CLI 贏。

## Paved-road reframe（別建 mega lifecycle operator）

- **bootstrap / BYO** = ArgoCD ApplicationSet golden-path（reuse 既有 charts + `operator_generate` + `onboard_platform`，明定 opinion + escape hatch）。
- **day2-drift** = 小 reconcile 擴充（延 `maintenance_scheduler` CronJob，level-triggered，非 watch-mode）。
- **CRD-as-API（≠ CRD-as-controller）** 只在有 kubectl-native 客戶才做（Gate 2）。
- Operator 維持 deferred（[ADR-008](../adr/008-operator-native-integration-path.md)）。
