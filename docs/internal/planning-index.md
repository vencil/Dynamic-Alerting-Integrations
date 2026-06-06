---
title: "Planning Index — Discovery-based Backlog View"
tags: [internal, dx, planning, derived-view]
audience: [contributors, ai-agents]
version: v2.8.1
lang: zh
---

# Planning Index — Discovery-based Backlog View

> **本文件為 derived view，由 `scripts/dx/generate_planning_index.py` 自動產生。**
>
> 修改 status / pr_ref 等請改 source file（各 backlog `.md` 的 frontmatter / 嵌入式 yaml block、`flaky-tests.yaml`、code 內 `// TECH-DEBT(id=...)` 註解）。改完跑 `make planning-index` 重新渲染；pre-commit drift gate `planning-index-check` 會擋 stale 表。
>
> Source 與 namespace 政策見 [ADR-019](../adr/019-planning-ssot.md)；legacy ID → TRK 對映見 [`planning-id-mapping.md`](planning-id-mapping.md)。
>
> 本檔由 `scripts/dx/generate_planning_index.py` 寫入；不要手動編輯哨點之間的內容。

## 索引

<!-- PLANNING_INDEX_START -->
### in-progress (2)

| ID | Kind | Title | Domain | PR | Source |
|----|------|-------|--------|------|--------|
| `TRK-006` | dx | TRK-006: Skip budget CI gate | ci | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |
| `TRK-010` | dx | TRK-010: Flake 自動重試 CI Policy（不是盲目全域 retry） | ci | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |

### accepted (24)

| ID | Kind | Title | Domain | PR | Source |
|----|------|-------|--------|------|--------|
| `ADR-001` | adr | ADR-001: 嚴重度 Dedup 採用 Inhibit 規則 | exporter | — | [docs/adr/001-severity-dedup-via-inhibit.md](../adr/001-severity-dedup-via-inhibit.md) |
| `ADR-002` | adr | ADR-002: OCI Registry 替代 ChartMuseum | helm | — | [docs/adr/002-oci-registry-over-chartmuseum.md](../adr/002-oci-registry-over-chartmuseum.md) |
| `ADR-003` | adr | ADR-003: Sentinel Alert 模式 | exporter | — | [docs/adr/003-sentinel-alert-pattern.md](../adr/003-sentinel-alert-pattern.md) |
| `ADR-004` | adr | ADR-004: Federation 架構——中央 Exporter 優先 | exporter | — | [docs/adr/004-federation-central-exporter-first.md](../adr/004-federation-central-exporter-first.md) |
| `ADR-005` | adr | ADR-005: 投影卷掛載 Rule Pack | k8s | — | [docs/adr/005-projected-volume-for-rule-packs.md](../adr/005-projected-volume-for-rule-packs.md) |
| `ADR-006` | adr | ADR-006: 租戶映射拓撲 (1:1, N:1, 1:N) | exporter | — | [docs/adr/006-tenant-mapping-topologies.md](../adr/006-tenant-mapping-topologies.md) |
| `ADR-007` | adr | ADR-007: 跨域路由設定檔與域策略 | tenant-api | — | [docs/adr/007-cross-domain-routing-profiles.md](../adr/007-cross-domain-routing-profiles.md) |
| `ADR-008` | adr | ADR-008: Operator-Native 整合路徑 | k8s | — | [docs/adr/008-operator-native-integration-path.md](../adr/008-operator-native-integration-path.md) |
| `ADR-009` | adr | ADR-009: Tenant Manager CRUD API 架構 | tenant-api | — | [docs/adr/009-tenant-manager-crud-api.md](../adr/009-tenant-manager-crud-api.md) |
| `ADR-010` | adr | ADR-010: Multi-Tenant Grouping Architecture | tenant-api | — | [docs/adr/010-multi-tenant-grouping.md](../adr/010-multi-tenant-grouping.md) |
| `ADR-011` | adr | ADR-011: PR-based Write-back 模式 | tenant-api | — | [docs/adr/011-pr-based-write-back.md](../adr/011-pr-based-write-back.md) |
| `ADR-012` | adr | ADR-012: threshold-heatmap 色盲補丁 — 結構化 severity 返回值 | portal | — | [docs/adr/012-colorblind-hotfix-structured-severity-return.md](../adr/012-colorblind-hotfix-structured-severity-return.md) |
| `ADR-013` | adr | ADR-013: Component Health Scanner — Tier 評分演算法與 token_density 輔助指標 | dx | — | [docs/adr/013-component-health-token-density-metric.md](../adr/013-component-health-token-density-metric.md) |
| `ADR-014` | adr | ADR-014: wizard.jsx design token 遷移採 Option A（Tailwind arbitrary value 全改寫） | portal | — | [docs/adr/014-wizard-arbitrary-value-token-migration.md](../adr/014-wizard-arbitrary-value-token-migration.md) |
| `ADR-015` | adr | ADR-015: 全面改用 `[data-theme]` 單軌 dark mode，移除 Tailwind `dark:` 變體 | portal | — | [docs/adr/015-data-theme-single-track-dark-mode.md](../adr/015-data-theme-single-track-dark-mode.md) |
| `ADR-016` | adr | ADR-016: conf.d/ 目錄分層 + 混合模式 + 遷移策略 | exporter | — | [docs/adr/016-conf-d-directory-hierarchy-mixed-mode.md](../adr/016-conf-d-directory-hierarchy-mixed-mode.md) |
| `ADR-017` | adr | ADR-017: _defaults.yaml 繼承語意 + dual-hash hot-reload | exporter | — | [docs/adr/017-defaults-yaml-inheritance-dual-hash.md](../adr/017-defaults-yaml-inheritance-dual-hash.md) |
| `ADR-018` | adr | ADR-018: Profile-as-Directory-Default | tools | — | [docs/adr/018-profile-as-directory-default.md](../adr/018-profile-as-directory-default.md) |
| `ADR-019` | adr | ADR-019: Planning SSOT — Frontmatter Contract + Discovery-based Index | docs | — | [docs/adr/019-planning-ssot.md](../adr/019-planning-ssot.md) |
| `ADR-020` | adr | ADR-020: Tenant Federation — Label-Injection Proxy over Self-Built Endpoint | tenant-api | — | [docs/adr/020-tenant-federation.md](../adr/020-tenant-federation.md) |
| `ADR-021` | adr | ADR-021: Tenant Log Query — Authorization-Plane-Only, Ingestion-Decoupled | tenant-api | — | [docs/adr/021-tenant-log-query-federation.md](../adr/021-tenant-log-query-federation.md) |
| `ADR-022` | adr | ADR-022: tenant-api Dev-Auth Bypass — Local-Dev Identity Substitute, Four-Layer Containment | tenant-api | — | [docs/adr/022-dev-auth-bypass-four-layer-containment.md](../adr/022-dev-auth-bypass-four-layer-containment.md) |
| `ADR-023` | adr | ADR-023: tenant-api 寫入平面 — Single-Writer Invariant 與韌性圍堵 | tenant-api | — | [docs/adr/023-write-plane-single-writer-invariant.md](../adr/023-write-plane-single-writer-invariant.md) |
| `ADR-024` | adr | ADR-024: 宣告式 Dimensional 告警引擎 — Version-Aware Thresholds + Custom Alerts | threshold-exporter | — | [docs/adr/024-version-aware-threshold-via-dimensional-label.md](../adr/024-version-aware-threshold-via-dimensional-label.md) |

### proposed (15)

| ID | Kind | Title | Domain | PR | Source |
|----|------|-------|--------|------|--------|
| `TRK-001` | dx | TRK-001: `check_noqa_hygiene.py` — noqa/nosec 必要性驗證 | ci | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |
| `TRK-002` | dx | TRK-002: `make test-impact` — 變更影響測試自動縮減 | ci | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |
| `TRK-003` | dx | TRK-003: Pre-commit hook CI gate | ci | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |
| `TRK-004` | dx | TRK-004: Lint tool self-test framework（negative fixtures） | ci | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |
| `TRK-005` | dx | TRK-005: `check_test_isolation.py` — 測試隔離驗證 | ci | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |
| `TRK-007` | dx | TRK-007: Lint test coverage 補齊（18 支缺測試） | ci | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |
| `TRK-008` | dx | TRK-008: CI ignore 文件化與 test-map 更新 | docs | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |
| `TRK-009` | dx | TRK-009: Coverage source 一致性 lint | ci | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |
| `TRK-012` | dx | TRK-012: ADR / 內部連結檔名一致性 Lint | docs | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |
| `TRK-013` | dx | TRK-013: Spoke 文件 Freshness Gate（防「空頭支票」） | docs | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |
| `TRK-014` | dx | TRK-014: FUSE-side Git Write 防護 Wrapper | ops | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |
| `TRK-015` | dx | TRK-015: Session 起手式 PATH+PATHEXT Smoke Test | ops | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |
| `TRK-016` | dx | TRK-016: CHANGELOG 計數一致性 Lint（tool count / JSX count / hook count） | ci | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |
| `TRK-017` | dx | TRK-017: Desktop Commander 長命令 Watchdog Wrapper | ops | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |
| `TRK-018` | dx | TRK-018: `engineering:testing-strategy` Skill 驅動的測試設計還債 | ci | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |

### done (1)

| ID | Kind | Title | Domain | PR | Source |
|----|------|-------|--------|------|--------|
| `TRK-011` | dx | TRK-011: Fake-Clock 注入（根因修復 Go 時間相依測試） | exporter | — | [docs/internal/dx-tooling-backlog.md](../internal/dx-tooling-backlog.md) |


<!-- PLANNING_INDEX_END -->

## 資料來源

| Source | 偵測方式 | 預期 entry 形態 |
|--------|----------|----------------|
| `docs/**/*.md` top-of-file frontmatter | `---` 區塊含 `tracking_kind:` 欄 | 整個檔案視為一個 planning item（典型用法：每個 ADR、每份獨立 spec） |
| `docs/**/*.md` 嵌入式 YAML | H2/H3 heading 後緊接 \`\`\`yaml ... \`\`\` 區塊含 `tracking_kind:` | 一份檔案多個 entry（典型用法：`dx-tooling-backlog.md` / `frontend-quality-backlog.md` 等批次清單）|
| `flaky-tests.yaml` 頂層 list | 每個 dict 含 `tracking_kind:` | flaky test 升級為正式追蹤項目時用 |
| Code-comment 註解 | `// TECH-DEBT(id=TRK-042, status=in-progress, tracking_kind=tech-debt)` 或 `# TECH-DEBT(...)`，逗號分隔 key=value | 直接埋在程式碼內的 inline tech-debt |

## 為什麼是 derived view（不是 SSOT）

SSOT 永遠在 source（各 backlog / yaml / code），index 是 grep + render 的快照。修改某 entry 的 `status:` 必須改 source；index 只是 review-friendly 的快查表。pre-commit hook 把這個 invariant 機械化擋住：source 改了沒重新 render 就 fail。

## 關聯

- [ADR-019 §Layer 2 — Discovery-based Index Generator](../adr/019-planning-ssot.md#三層設計) — 本工具的 design rationale
- [`planning-id-mapping.md`](planning-id-mapping.md) — legacy ID → TRK 對映表
- [`dev-rules.md` §P1](dev-rules.md) — commit trailer 規範
- [issue #379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) — Planning SSOT system implementation epic（本工具為 chunk 2a）
