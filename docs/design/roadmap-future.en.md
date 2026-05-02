---
title: "Future Roadmap — K8s Operator, Design System, Auto-Discovery, and More"
tags: [architecture, roadmap, design]
audience: [platform-engineer, devops]
version: v2.7.0
lang: en
parent: architecture-and-design.en.md
---
# Future Roadmap

> **Language / 語言：** **English (Current)** | [中文](roadmap-future.md)
>
> ← [Back to Main Document](../architecture-and-design.en.md)

DX tooling improvements are tracked in [dx-tooling-backlog.md](../internal/dx-tooling-backlog.md).

---

## v2.8.0 Delivered (Phase .a/.b/.c/.d; Phase .e release wrap-up in progress)

The Scale Foundation I laid in v2.7.0 (`conf.d/` hierarchy + `_defaults.yaml` inheritance + dual-hash + `/effective`) + component robustness (Design Token migration across 9 JSX tools + Component Health + dark mode ADR-016) + test infrastructure (1000-tenant fixture + Blast Radius CI bot) evolved in v2.8.0 into a **complete customer-deployable pipeline + Scale production validation + automation consolidation**.

### Customer Migration Pipeline (Phase .c) — 5-step chain ✅

End-to-end flow that imports a customer's existing PromRule corpus into this platform's conf.d/ architecture, codified entirely as offline-runnable Go binaries:

```
PromRule corpus → da-parser → da-tools profile build → da-batchpr apply → da-guard → conf.d/
```

- **`da-parser`** (C-8): Dialect detection (prom / metricsql / ambiguous) + VM-only function allowlist (`vm_only_functions.yaml` via `go:embed`, CI freshness gate detects new metricsql upstream functions) + `StrictPromQLValidator` + provenance header (`generated_by` / `source_rule_id` / `parsed_at` / `source_checksum`). The `prom_portable: bool` flag lets customers identify the "can-go-back-to-Prom" subset after migrating to VM — a concrete anti-vendor-lock-in commitment.
- **`da-tools profile build`** (C-9): Cluster similar rules → median algorithm picks the cluster's shared threshold → write `_defaults.yaml`, deviating tenants write `<id>.yaml` containing override-only keys; opt-in fuzzy matching applies duration-equivalence canonicalisation (`[5m]` ≡ `[300s]` ≡ `[300000ms]`); follows [ADR-019](../adr/019-profile-as-directory-default.en.md) Profile-as-Directory-Default.
- **`da-batchpr apply`** (C-10): Hierarchy-Aware chunking — `_defaults.yaml` changes go in a Base Infrastructure PR; per-tenant PRs marked `Blocked by:`. `refresh --base-merged` auto-rebases downstream after Base merge; `refresh --source-rule-ids` regenerates patch PRs at fine granularity for parser bug fixes.
- **`da-guard`** (C-12): Schema / Routing / Cardinality / Redundant-override 4-layer check; `.github/workflows/guard-defaults-impact.yml` runs automatically + posts sticky PR comment (marker-based update vs create) + uploads artifact with 14d retention.

### Scale Foundation III + Tenant API hardening (Phase .b) ✅

- 1000-tenant synthetic baseline landed: `make benchmark-report` runs 17 benches × count=6 nightly via cron; mixed-mode flat+hierarchy benches added to trend tracking.
- Tenant API hardening: rate limit per-pod + `X-Request-ID` middleware + tenant-scoped authz + body-content range validation (go-playground/validator + struct tags + reservedKeyValidators registry).
- Mixed-mode duplicate tenant ID: WARN → typed `*DuplicateTenantError` hard error + state preservation invariant.

### Server-side Search / Tenant Manager virtualization / Master Onboarding / Smart Views (Phase .c) ✅

- **C-1** `GET /api/v1/tenants/search`: page_size cap 500 + closed-field free-text + RBAC-before-pagination + 30s TTL `tenantSnapshotCache`; p99 < 200ms @ 1000 tenants.
- **C-2** Tenant Manager JSX: API-first 3-layer priority chain (API → platform-data.json → DEMO) + 429 retry-with-backoff + server-side `q` filter (debounced 300ms) + URL state (`useURLState` + `useDebouncedValue`) + self-written `useVirtualGrid` (only virtualizes when `filtered.length > 50`; the customer 500+ tenant DOM-freeze problem is solved at the server-cap layer).
- **C-3** Master Onboarding Dual Entry: Import Journey 5 steps (C-8/9/10/12 inline CLI) vs Wizard Journey 5 steps (cicd-setup → deployment → alert-builder → routing-trace → tenant-manager — all 5/5 real wizards).
- **C-4** Tenant Manager × Wizard integration: TenantCard footer 3 buttons (Alert / Route / Preview) deep link + `?tenant_id=` URL param pre-fill + standalone `simulate-preview.jsx` widget (4-state machine + 500ms debounce + AbortController).
- **C-6** Smart Views: `useSavedViews` + `SavedViewsPanel` wires to v2.5.0 backend `/api/v1/views` CRUD; RBAC-aware (Save/Delete hidden when `canWrite=false`).

### Migration Toolkit packaging + supply-chain provenance (Phase .c, C-11) ✅

- Three delivery paths in parallel: (a) Docker pull `ghcr.io/vencil/da-tools` (b) Static binary linux/darwin/windows × amd64/arm64 — 6 archives (c) Air-gapped tar (`docker save` export).
- Layer 1 delivered: cosign keyless signing (OIDC identity pinned) + SBOM SPDX/CycloneDX dual-format (also signed) + one-shot customer helper `make verify-release`.
- Layer 2/3 (GPG / Authenticode / HSM / FIPS / SLSA L2-3 / reproducible / in-toto) reserved for customer-RFP-driven activation; runbook is written.
- See: [Migration Toolkit Installation](../migration-toolkit-installation.en.md) · [Release Signing Runbook](../internal/release-signing-runbook.md).

### Phase .d ZH-primary policy lock ✅

The v2.5.0 evaluation §7 originally recommended switching to EN SSOT; Phase 1 pilot tooling completed in v2.7.0. v2.8.0 S#101 applied the `testing-playbook §LL §12a` 4-question audit (**Q4 NEW: spec premise validation**) and reversed the original plan: the "open-source SSOT should be EN" premise was never validated against the actual contributor pool → strong fail → no full ZH→EN migration. Phase 1 tooling kept dormant with explicit codified trigger conditions (≥3 non-Chinese-native contributors / customer RFP explicitly requires EN / maintainer pivots to international-positioning project).

### Policy-as-Code automation (Phase .a / accumulated across PRs) ✅

Upgraded "text rule → reviewer convention → AI reminder" to lint hooks that auto-block: `check_hardcode_tenant.py` (Rule #2 PromQL label selector) / `check_dev_rules_enforcement.py` (auto-detects dev-rules ↔ pre-commit drift) / `check_subprocess_timeout.py` (Layer A, FATAL activated) / `check_jsx_loader_compat.py` (named-export / non-allowlist-import / require-call) / `check_playwright_rtl_drift.py` (RTL `getByDisplayValue` family in Playwright specs) / `check_undefined_tokens.py` (incl. `--report-orphans`) / `check_changelog_no_tbd.py` (CHANGELOG placeholders) / `check_ad_hoc_git_scripts.py` (Trap #54 enforcement) / `scaffold_lint.py + make lint-extract` (5-kind template; next lint ~15 min). 56 hooks total: 39 auto + 14 manual + 3 pre-push.

---

## Phase .e Remaining (v2.8.0 release wrap-up)

- ⬜ Real 4-hr soak (`make soak-readiness`, produces `.build/v2.8.0-soak/soak-report.md` as a release asset)
- ⬜ `make pre-tag` (version-check + lint-docs; `make bump-docs` flips v2.7.0 → v2.8.0 across 50+ docs in one shot)
- ⬜ `make benchmark-report` for v2.8.0 baseline
- ⬜ Draft v2.8.0 GitHub Release body ([github-release-playbook.md §Step 3.5](../internal/github-release-playbook.md) skeleton + planning archive §1/§2/§3 distill)
- ⬜ Five-line tag push (`v2.8.0` / `exporter/v2.8.0` / `tools/v2.8.0` / `portal/v2.8.0` / `tenant-api/v2.8.0`) + Release publish

---

## Deferred to v2.9.0 (explicitly out of v2.8.0 scope)

| Item | Why deferred | Tracking |
|---|---|---|
| **EN-first Bilingual SSOT — Full Migration** | Phase .d S#101 reverse — premise (open-source should be EN) was never validated; existing customers and contributors are all Chinese-native | [#145](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/145) re-evaluation triggers codified |
| **Field-level RBAC** | v2.8.0 prioritizes the customer migration pipeline; RBAC split needs middleware + OpenAPI + Portal UI three-layer changes — large scope | Pair with v2.9.0 customer hardening pass 2 |
| **Tenant Auto-Discovery** | Needs sidecar mode design + `discover_instance_mappings.py` rework, scope-orthogonal to v2.8.0 customer migration pipeline | Driven by first onboarding customer's actual needs |
| **Grafana Dashboard as Code** | Needs `scaffold_tenant.py --grafana` and platform-data.json rework | Exploratory; no customer hard ask |
| **Customer onboarding hardening pass 2** | 4-hr soak / customer-anon corpus / migration playbook walkthrough rehearsal | [#140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/140) / [#141](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/141) / [#142](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/142) |
| **tenant-api remaining items** | SSE per-client idle timeout / server timeout + body-size moved to Helm value | [#143](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/143) / [#144](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/144) |
| **Mixed-mode authoritative perf characterization** | Needs 28+ nightly bench-record data points (wall-clock-bound) | [#128](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/128) |
| **Pre-tag bench gate Phase 2** | Needs main-only hard gate + Larger Runners | [#67](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/67) |

---

## Exploratory (Long-term)

| Direction | Prerequisites | Expected Value |
|-----------|--------------|----------------|
| **Anomaly-Aware Dynamic Threshold** | ML infrastructure (time-series analysis, seasonality detection) | Thresholds evolve from "manually set" to "auto-adaptive". `_threshold_mode: adaptive` + `quantile_over_time`. Static thresholds as safety floor |
| **Log-to-Metric Bridge** | Loki / Elasticsearch integration | Unified log + metric alert management. Recommended: `grok_exporter / mtail → Prometheus → this platform` |
| **Multi-Format Export** | metric-dictionary.yaml mapping table | `da-tools export --format datadog/terraform` — platform becomes alert policy abstraction layer |
| **DynamicAlertTenant CRD** | Operator SDK + CRD versioning | Replace ConfigMap + Directory Scanner (requires re-evaluating ADR-008 boundaries) |
| **ChatOps Deep Integration** | Slack/Teams Bot SDK | Bidirectional operations (query tenant status, trigger silent mode) |
| **CI/CD Pipeline Status Pass-through** | PR write-back stabilization | PR/MR CI Status Check feedback to Portal UI |
| **SRE Alert Tracker** | Alert lifecycle model design | Trigger → Acknowledge → Investigate → Resolve → Postmortem |

---

## Version Evolution

| Version | Theme | Milestones |
|---------|-------|-----------|
| v2.7.0 | Scale Foundation × Component Robustness × Test Infrastructure | ADR-012~018 (7 new), `conf.d/` hierarchy + `_defaults.yaml` inheritance, dual-hash hot-reload, `/effective` endpoint, 5-dim Component Health, Design Token migration across 9 JSX tools |
| v2.6.0 | Operator × PR Write-back × Design System | ADR-011, GitLab MR, axe-core WCAG |
| v2.5.0 | Multi-Tenant Grouping × E2E Testing | Playwright foundation, Saved Views |
| v2.4.0 | Tenant Management API × pkg/config | REST API RBAC, Portal UI |
| v2.3.0 | Operator Native Path × Rule Pack Split | ADR-008, federation-check, rule-pack-split |
| v2.2.0 | Adoption Pipeline × CLI Extension | init, config-history, gitops-check |
| v2.1.0 | Routing Profiles × Domain Policy | ADR-007, four-layer routing merge |

Full version history: [CHANGELOG.md](../CHANGELOG.md).
