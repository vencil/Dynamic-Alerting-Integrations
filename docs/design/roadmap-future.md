---
title: "未來擴展路線 — K8s Operator、Design System、Auto-Discovery 等"
tags: [architecture, roadmap, design]
audience: [platform-engineer, devops]
version: v2.7.0
parent: architecture-and-design.md
lang: zh
---
# 未來擴展路線

> **Language / 語言：** **中文 (Current)** | [English](./roadmap-future.en.md)

<!-- Language switcher is provided by mkdocs-static-i18n header. -->

> ← [返回主文件](../architecture-and-design.md)

DX 工具改善追蹤見 [dx-tooling-backlog.md](../internal/dx-tooling-backlog.md)。

---

## v2.8.0 已交付（Phase .a/.b/.c/.d，Phase .e release 收尾中）

v2.7.0 奠定的 Scale Foundation I（`conf.d/` 階層 + `_defaults.yaml` 繼承 + dual-hash + `/effective`）+ 元件健壯化（Design Token 9 支 JSX + Component Health + dark mode ADR-016）+ 測試基礎設施（1000-tenant fixture + Blast Radius CI bot），在 v2.8.0 進化為**客戶可導入的完整 pipeline + Scale 生產驗證 + 自動化收斂**。

### 客戶導入管線 (Phase .c) — 5-step chain ✅

把客戶既有的 PromRule corpus 導入到本平台 conf.d/ 架構的端到端流程，全部 codify 為可離線執行的 Go binary：

```
PromRule corpus → da-parser → da-tools profile build → da-batchpr apply → da-guard → conf.d/
```

- **`da-parser`** (C-8)：dialect 偵測（prom / metricsql / ambiguous）+ VM-only 函數 allowlist（`vm_only_functions.yaml` 走 `go:embed`，CI freshness gate 偵測新版 metricsql 上游函數）+ `StrictPromQLValidator` + provenance header（`generated_by` / `source_rule_id` / `parsed_at` / `source_checksum`）。`prom_portable: bool` 旗標讓客戶遷入 VM 後仍能識別「可回 Prom」的子集 — anti-vendor-lock-in 具體承諾
- **`da-tools profile build`** (C-9)：cluster 相似 rules → median 演算法決定 cluster 共通閾值 → 寫 `_defaults.yaml`、偏離 tenant 寫 `<id>.yaml` 只含 override；fuzzy matching opt-in 套 duration-equivalence canonicalisation（`[5m]` ≡ `[300s]` ≡ `[300000ms]`）；遵循 [ADR-019](../adr/019-profile-as-directory-default.md) Profile-as-Directory-Default
- **`da-batchpr apply`** (C-10)：Hierarchy-Aware 分塊 — `_defaults.yaml` 變更打 Base Infrastructure PR、tenant PRs 標 `Blocked by:`；`refresh --base-merged` 在 Base merge 後自動 rebase 下游；`refresh --source-rule-ids` 對 parser bug fix 細粒度重生 patch PR
- **`da-guard`** (C-12)：Schema / Routing / Cardinality / Redundant-override 四層檢查；`.github/workflows/guard-defaults-impact.yml` 自動跑 + sticky PR comment（marker-based update vs create）+ artifact 14d retention

### Scale Foundation III + Tenant API hardening (Phase .b) ✅

- 1000-tenant synthetic baseline land：`make benchmark-report` 17 benches × count=6 跑 nightly cron；mixed-mode flat+hierarchy benches 加入 trend tracking
- Tenant API hardening：rate limit per-pod + `X-Request-ID` middleware + tenant-scoped authz + body-content range validation（go-playground/validator + struct tags + reservedKeyValidators registry）
- Mixed-mode duplicate tenant ID：WARN → typed `*DuplicateTenantError` hard error + state preservation invariant

### Server-side Search / Tenant Manager virtualization / Master Onboarding / Smart Views (Phase .c) ✅

- **C-1** `GET /api/v1/tenants/search`：page_size cap 500 + closed-field free-text + RBAC-before-pagination + 30s TTL `tenantSnapshotCache`，p99 < 200ms @ 1000T
- **C-2** Tenant Manager JSX：API-first 三層 priority chain（API → platform-data.json → DEMO）+ 429 retry-with-backoff + server-side `q` filter（debounced 300ms）+ URL state（`useURLState` + `useDebouncedValue`）+ self-written `useVirtualGrid`（`filtered.length > 50` 才 virtualize；客戶 500+ tenant DOM-freeze 在 server-cap 層解掉）
- **C-3** Master Onboarding Dual Entry：Import Journey 5 步（C-8/9/10/12 inline CLI）vs Wizard Journey 5 步（cicd-setup → deployment → alert-builder → routing-trace → tenant-manager 全 5/5 真 wizards）
- **C-4** Tenant Manager × Wizard 整合：TenantCard footer 三鈕（Alert / Route / Preview）deep link + `?tenant_id=` URL 參數預填 + 獨立 `simulate-preview.jsx` widget（4-state machine + 500ms debounce + AbortController）
- **C-6** Smart Views：`useSavedViews` + `SavedViewsPanel` 接 v2.5.0 backend `/api/v1/views` CRUD；RBAC-aware（Save/Delete hidden when `canWrite=false`）

### Migration Toolkit packaging + supply-chain provenance (Phase .c, C-11) ✅

- 三條交付路徑並行：(a) Docker pull `ghcr.io/vencil/da-tools` (b) Static binary linux/darwin/windows × amd64/arm64 共 6 archives (c) Air-gapped tar (`docker save` export)
- Layer 1 已交付：cosign keyless 簽（OIDC identity pinned）+ SBOM SPDX/CycloneDX 雙格式也加簽 + `make verify-release` 客戶一鍵驗
- Layer 2/3（GPG / Authenticode / HSM / FIPS / SLSA L2-3 / reproducible / in-toto）保留 customer-RFP-driven activation path，runbook 已寫
- 詳：[Migration Toolkit Installation](../migration-toolkit-installation.md) · [Release Signing Runbook](../internal/release-signing-runbook.md)

### Phase .d ZH-primary policy lock ✅

v2.5.0 評估文 §7 原推薦切換 EN SSOT，Phase 1 試點工具於 v2.7.0 完成；v2.8.0 S#101 套 `testing-playbook §LL §12a` 4-question audit（**Q4 NEW: spec premise validation**）後 reverse 原計畫：「open-source SSOT 該 EN」premise 從未被 actual contributor pool 驗證 → strong fail → 不執行 ZH→EN 全量遷移。Phase 1 工具保留 dormant，trigger conditions 明確 codify（≥3 非中文母語 contributor / 客戶 RFP 顯式要求 EN / maintainer 主動 pivot international-positioning）。

### Policy-as-Code 自動化（Phase .a/各 PR 累積）✅

從「文字規範 → reviewer convention → AI 提醒」升級為 lint hook 自動攔截：`check_hardcode_tenant.py`（Rule #2 PromQL label selector）/ `check_dev_rules_enforcement.py`（dev-rules ↔ pre-commit 自動偵 drift）/ `check_subprocess_timeout.py`（Layer A，FATAL 已啟動）/ `check_jsx_loader_compat.py`（named-export / non-allowlist-import / require-call 三類）/ `check_playwright_rtl_drift.py`（RTL `getByDisplayValue` 系列在 Playwright spec）/ `check_undefined_tokens.py`（含 `--report-orphans` 模式）/ `check_changelog_no_tbd.py`（CHANGELOG placeholder）/ `check_ad_hoc_git_scripts.py`（Trap #54 enforcement）/ `scaffold_lint.py + make lint-extract`（5-kind template，下個 lint ~15 min）。56 hooks 共 39 auto + 14 manual + 3 pre-push。

---

## Phase .e 待跑（v2.8.0 release 收尾）

- ⬜ 真實 4-hr soak（`make soak-readiness`，產出 `.build/v2.8.0-soak/soak-report.md` 作 release asset）
- ⬜ `make pre-tag`（version-check + lint-docs；`make bump-docs` 統一 v2.7.0 → v2.8.0 跨 50+ 文件）
- ⬜ `make benchmark-report` 取 v2.8.0 baseline
- ⬜ 起草 v2.8.0 GitHub Release body（[github-release-playbook.md §Step 3.5](../internal/github-release-playbook.md) skeleton + planning archive §1/§2/§3 distill）
- ⬜ 五線 tag 推送（`v2.8.0` / `exporter/v2.8.0` / `tools/v2.8.0` / `portal/v2.8.0` / `tenant-api/v2.8.0`）+ Release publish

---

## v2.8.0 已 deferred 至 v2.9.0（明確不在本版範圍）

| 項目 | 為何延 | tracking |
|---|---|---|
| **EN-first 雙語 SSOT 全量遷移** | Phase .d S#101 reverse — premise（open-source 該 EN）從未驗證；既有客戶與 contributor 均中文母語 | [#145](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/145) re-evaluation 觸發條件已 codify |
| **Field-level RBAC** | v2.8.0 重心放客戶導入管線；RBAC 拆分需 middleware + OpenAPI + Portal UI 三層改動，scope 大 | 留 v2.9.0 customer hardening pass 2 一起做 |
| **Tenant Auto-Discovery** | 需要 sidecar 模式設計 + `discover_instance_mappings.py` 改造，跟 v2.8.0 客戶導入管線 scope 不重疊 | 視第一個導入客戶實際需求決定 |
| **Grafana Dashboard as Code** | 需 `scaffold_tenant.py --grafana` 與 platform-data.json 改造 | 探索方向；無客戶 hard ask |
| **Customer onboarding hardening pass 2** | 4-hr soak / customer-anon corpus / migration playbook walkthrough rehearsal | [#140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/140) / [#141](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/141) / [#142](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/142) |
| **tenant-api 補完** | SSE per-client idle timeout / server timeout + body-size 移到 Helm value | [#143](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/143) / [#144](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/144) |
| **Mixed-mode 性能 authoritative characterization** | 需 28+ nightly bench-record data points（wall-clock-bound）| [#128](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/128) |
| **Pre-tag bench gate Phase 2** | 需 main-only hard gate + Larger Runners | [#67](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/67) |

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
| v2.7.0 | Scale Foundation × 元件健壯化 × 測試基礎設施 | ADR-012~018（7 新）、`conf.d/` 階層 + `_defaults.yaml` 繼承、dual-hash hot-reload、`/effective` endpoint、Component Health 5-dim、Design Token 9 支 JSX |
| v2.6.0 | Operator × PR Write-back × Design System | ADR-011、GitLab MR、axe-core WCAG |
| v2.5.0 | Multi-Tenant Grouping × E2E Testing | Playwright 基礎、Saved Views |
| v2.4.0 | Tenant Management API × pkg/config | REST API RBAC、Portal UI |
| v2.3.0 | Operator Native Path × Rule Pack Split | ADR-008、federation-check、rule-pack-split |
| v2.2.0 | Adoption Pipeline × CLI 擴展 | init、config-history、gitops-check |
| v2.1.0 | Routing Profiles × Domain Policy | ADR-007、四層路由合併 |

完整版本歷程見 [CHANGELOG.md](../CHANGELOG.md)。
