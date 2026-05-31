---
title: "未來擴展路線 — K8s Operator、Design System、Auto-Discovery 等"
tags: [architecture, roadmap, design]
audience: [platform-engineer, devops]
version: v2.8.1
parent: architecture-and-design.md
lang: zh
---
# 未來擴展路線

> **Language / 語言：** **中文 (Current)** | [English](./roadmap-future.en.md)

<!-- Language switcher is provided by mkdocs-static-i18n header. -->

> ← [返回主文件](../architecture-and-design.md)

DX 工具改善追蹤見 [dx-tooling-backlog.md](../internal/dx-tooling-backlog.md)。

---

## v2.8.0 已交付（release 收尾中）

v2.7.0 奠定的 Scale Foundation I（`conf.d/` 階層 + `_defaults.yaml` 繼承 + dual-hash + `/effective`）+ 元件健壯化（Design Token 9 支 JSX + Component Health + dark mode ADR-015）+ 測試基礎設施（1000-tenant fixture + Blast Radius CI bot），在 v2.8.0 進化為**客戶可導入的完整 pipeline + Scale 生產驗證 + 自動化收斂**。

### 客戶導入管線 — 5-step chain ✅

把客戶既有的 PromRule corpus 導入到本平台 conf.d/ 架構的端到端流程，全部 codify 為可離線執行的 Go binary：

```
PromRule corpus → da-parser → da-tools profile build → da-batchpr apply → da-guard → conf.d/
```

- **`da-parser`**：dialect 偵測（prom / metricsql / ambiguous）+ VM-only 函數 allowlist（`vm_only_functions.yaml` 走 `go:embed`，CI freshness gate 偵測新版 metricsql 上游函數）+ `StrictPromQLValidator` + provenance header（`generated_by` / `source_rule_id` / `parsed_at` / `source_checksum`）。`prom_portable: bool` 旗標讓客戶遷入 VM 後仍能識別「可回 Prom」的子集 — anti-vendor-lock-in 具體承諾
- **`da-tools profile build`**：cluster 相似 rules → median 演算法決定 cluster 共通閾值 → 寫 `_defaults.yaml`、偏離 tenant 寫 `<id>.yaml` 只含 override；fuzzy matching opt-in 套 duration-equivalence canonicalisation（`[5m]` ≡ `[300s]` ≡ `[300000ms]`）；遵循 [ADR-018](../adr/018-profile-as-directory-default.md) Profile-as-Directory-Default
- **`da-batchpr apply`**：Hierarchy-Aware 分塊 — `_defaults.yaml` 變更打 Base Infrastructure PR、tenant PRs 標 `Blocked by:`；`refresh --base-merged` 在 Base merge 後自動 rebase 下游；`refresh --source-rule-ids` 對 parser bug fix 細粒度重生 patch PR
- **`da-guard`**：Schema / Routing / Cardinality / Redundant-override 四層檢查；`.github/workflows/guard-defaults-impact.yml` 自動跑 + sticky PR comment（marker-based update vs create）+ artifact 14d retention

### Scale Foundation III + Tenant API hardening ✅

- 1000-tenant synthetic baseline land：`make benchmark-report` 17 benches × count=6 跑 nightly cron；mixed-mode flat+hierarchy benches 加入 trend tracking
- Tenant API hardening：rate limit per-pod + `X-Request-ID` middleware + tenant-scoped authz + body-content range validation（go-playground/validator + struct tags + reservedKeyValidators registry）
- Mixed-mode duplicate tenant ID：WARN → typed `*DuplicateTenantError` hard error + state preservation invariant

### Server-side Search / Tenant Manager virtualization / Master Onboarding / Smart Views ✅

- **Server-side Search API** `GET /api/v1/tenants/search`：page_size cap 500 + closed-field free-text + RBAC-before-pagination + 30s TTL `tenantSnapshotCache`，p99 < 200ms @ 1000T
- **Tenant Manager JSX**：API-first 三層 priority chain（API → platform-data.json → DEMO）+ 429 retry-with-backoff + server-side `q` filter（debounced 300ms）+ URL state（`useURLState` + `useDebouncedValue`）+ self-written `useVirtualGrid`（`filtered.length > 50` 才 virtualize；客戶 500+ tenant DOM-freeze 在 server-cap 層解掉）
- **Master Onboarding Dual Entry**：Import Journey 5 步（parser / profile build / batch-pr / guard inline CLI）vs Wizard Journey 5 步（cicd-setup → deployment → alert-builder → routing-trace → tenant-manager 全 5/5 真 wizards）
- **Tenant Manager × Wizard 整合**：TenantCard footer 三鈕（Alert / Route / Preview）deep link + `?tenant_id=` URL 參數預填 + 獨立 `simulate-preview.jsx` widget（4-state machine + 500ms debounce + AbortController）
- **Smart Views**：`useSavedViews` + `SavedViewsPanel` 接 v2.5.0 backend `/api/v1/views` CRUD；RBAC-aware（Save/Delete hidden when `canWrite=false`）

### Migration Toolkit packaging + supply-chain provenance ✅

- 三條交付路徑並行：(a) Docker pull `ghcr.io/vencil/da-tools` (b) Static binary linux/darwin/windows × amd64/arm64 共 6 archives (c) Air-gapped tar (`docker save` export)
- Layer 1 已交付：cosign keyless 簽（OIDC identity pinned）+ SBOM SPDX/CycloneDX 雙格式也加簽 + `make verify-release` 客戶一鍵驗
- Layer 2/3（GPG / Authenticode / HSM / FIPS / SLSA L2-3 / reproducible / in-toto）保留 customer-RFP-driven activation path，runbook 已寫
- 詳：[Migration Toolkit Installation](../migration-toolkit-installation.md) · [Release Signing Runbook](../internal/release-signing-runbook.md)

### ZH-primary SSOT policy lock ✅

v2.5.0 評估文 §7 原推薦切換 EN SSOT，pilot 工具於 `v2.7.0` 完成；v2.8.0 套 `testing-playbook §LL §12a` 4-question audit（**Q4 NEW: spec premise validation**）後 reverse 原計畫：「open-source SSOT 該 EN」premise 從未被 actual contributor pool 驗證 → strong fail → 不執行 ZH→EN 全量遷移。Pilot 工具保留 dormant，trigger conditions 明確 codify（≥3 非中文母語 contributor / 客戶 RFP 顯式要求 EN / maintainer 主動 pivot international-positioning）。

### Policy-as-Code 自動化（各 PR 累積）✅

從「文字規範 → reviewer convention → AI 提醒」升級為 lint hook 自動攔截：`check_hardcode_tenant.py`（Rule #2 PromQL label selector）/ `check_dev_rules_enforcement.py`（dev-rules ↔ pre-commit 自動偵 drift）/ `check_subprocess_timeout.py`（Layer A，FATAL 已啟動）/ `check_jsx_loader_compat.py`（named-export / non-allowlist-import / require-call 三類）/ `check_playwright_rtl_drift.py`（RTL `getByDisplayValue` 系列在 Playwright spec）/ `check_undefined_tokens.py`（含 `--report-orphans` 模式）/ `check_changelog_no_tbd.py`（CHANGELOG placeholder）/ `check_ad_hoc_git_scripts.py`（Trap #54 enforcement）/ `scaffold_lint.py + make lint-extract`（5-kind template，下個 lint ~15 min）。56 hooks 共 39 auto + 14 manual + 3 pre-push。

---

## Release 收尾待跑（v2.8.0）

- ⬜ 真實 4-hr soak（`make soak-readiness`，產出 `.build/v2.8.0-soak/soak-report.md` 作 release asset）
- ⬜ `make pre-tag`（version-check + lint-docs；`make bump-docs` 統一 v2.7.0 → v2.8.0 跨 50+ 文件）
- ⬜ `make benchmark-report` 取 v2.8.0 baseline
- ⬜ 起草 v2.8.0 GitHub Release body（[github-release-playbook.md §Step 3.5](../internal/github-release-playbook.md) skeleton + planning archive §1/§2/§3 distill）
- ⬜ 五線 tag 推送（`v2.8.0` / `exporter/v2.8.0` / `tools/v2.8.0` / `portal/v2.8.0` / `tenant-api/v2.8.0`）+ Release publish

---

## v2.9.0 發展方向：生產級硬化 (Customer Hardening)

v2.9.0 的開發節奏由「功能堆疊」轉向「實戰硬化」。根據首批導入客戶的反饋，對平台進行深度的品質磨光與穩定性驗證。

**即時追蹤** — 所有開發任務、Bug 修正、進度，請直接看 GitHub Milestone（避免文件版本與 issue 真相不一致）：

👉 **[v2.9.0 Milestone — 客戶實戰硬化](https://github.com/vencil/Dynamic-Alerting-Integrations/milestone/1)**

**核心聚焦領域：**

- **穩定性硬化** — 4-hr soak / 真實客戶 corpus 校準 / 更嚴格的 release-time bench gate（main-only hard gate + Larger Runners）
- **權限精細化** — Field-level RBAC 支援更複雜的企業組織授權模型
- **安全治理進階** — Glossary-driven codename gate Layer 2（self-healing；取代正向列舉的 whack-a-mole 模式）
- **維運自動化** — Rule Pack × threshold-calculator 資料流：[#457](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/457) R0 已拍板 → **採納 STAGE-1** `threshold_recommend --export-patch`（吐 conf.d patch）+ **延後**全自動 PR adapter（trigger：客戶 toil / RFP / maintainer poll）+ **不做**寫回 rule pack schema；論證見 [評估書](../internal/dec-f-457-r0-assessment.md) / Local try-it-yourself onboarding（exporter / tenant-api / portal / da-tools standalone）
- **遷移工具補完** — tenant-api SSE per-client idle timeout / server timeout 與 body-size 移到 Helm value

---

## 探索方向（長期）

| 方向 | 前置條件 | 預期價值 |
|------|---------|---------|
| **Anomaly-Aware Dynamic Threshold** | ML 基礎設施（時序分析、季節性偵測） | 閾值從「人工設定」進化為「自動調適」。`_threshold_mode: adaptive` + `quantile_over_time`。靜態閾值作為安全下限（floor） |
| **Log-to-Metric Bridge** | Loki / Elasticsearch 整合 | 統一 log + metric 告警管理。推薦生態系解法：`grok_exporter / mtail → Prometheus → 本平台` |
| **Multi-Format Export** | metric-dictionary.yaml 對照表 | `da-tools export --format datadog/terraform` — 平台成為告警策略的抽象層 |
| **DynamicAlertTenant CRD** | Operator SDK + CRD versioning | 取代 ConfigMap + Directory Scanner（需重新評估 ADR-008 架構邊界） |
| **ChatOps 深度整合** | Slack/Teams Bot SDK | 雙向操作（查詢 tenant 狀態、觸發靜默模式） |
| **CI/CD Pipeline 狀態透傳** | PR write-back 穩定化 | PR/MR CI Status Check 回傳 Portal UI |
| **SRE Alert Tracker** | 告警生命週期模型設計 | 觸發 → 認領 → 調查 → 解決 → 事後分析 |

---

## 版本演進紀錄

| 版本 | 主題 | 里程碑 |
|------|------|--------|
| v2.7.0 | Scale Foundation × 元件健壯化 × 測試基礎設施 | ADR-012~017（6 新）、`conf.d/` 階層 + `_defaults.yaml` 繼承、dual-hash hot-reload、`/effective` endpoint、Component Health 5-dim、Design Token 9 支 JSX |
| v2.6.0 | Operator × PR Write-back × Design System | ADR-011、GitLab MR、axe-core WCAG |
| v2.5.0 | Multi-Tenant Grouping × E2E Testing | Playwright 基礎、Saved Views |
| v2.4.0 | Tenant Management API × pkg/config | REST API RBAC、Portal UI |
| v2.3.0 | Operator Native Path × Rule Pack Split | ADR-008、federation-check、rule-pack-split |
| v2.2.0 | Adoption Pipeline × CLI 擴展 | init、config-history、gitops-check |
| v2.1.0 | Routing Profiles × Domain Policy | ADR-007、四層路由合併 |

完整版本歷程見 [CHANGELOG.md](../CHANGELOG.md)。
