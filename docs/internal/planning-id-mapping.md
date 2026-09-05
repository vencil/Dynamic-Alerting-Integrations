---
title: "Planning ID Mapping — Legacy → TRK Redirect"
tags: [internal, dx, planning, redirect]
audience: [contributors, ai-agents]
version: v2.9.0
lang: zh
---

# Planning ID Mapping — Legacy → TRK Redirect

> **本文件用途**：[ADR-019](../adr/019-planning-ssot.md) 採 **Option C refined hybrid**——把舊的 `TECH-DEBT-NNN` / `TD-NN` / `HA-NN` / `REG-NNN` 四個 namespace 統一為單一 `TRK-NNN`。本文是 **redirect 表**：當你在 commit / PR / 文件 / external citation 看到舊 ID，到這裡查對應的現代 `TRK-NNN`。
>
> **這不是 backlog**，只是 ID 翻譯表。Backlog 本體（current status / pr_ref / owner）放在各自 source frontmatter，最終透過 `scripts/dx/generate_planning_index.py`（issue [#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 2a）匯出 `planning-index.md`。

## Namespace 政策回顧

| Namespace | 用途 | v2.8.1-dx-interim 後狀態 |
|---|---|---|
| **TRK-NNN** | 統一 debt / regression / dx tracking | **唯一新增進入點**（v2.8.1+ 一律 TRK） |
| **ADR-NNN** | 架構設計決策史 | 保留（不參與本 mapping）|
| **S#NNN** | Sprint planning ledger | 保留（不參與本 mapping）|
| ~~`TECH-DEBT-NNN`~~ | (legacy) | 凍結；本表查對應 TRK |
| ~~`TD-NN`~~ | (legacy 簡寫) | 凍結；本表查對應 TRK |
| ~~`HA-NN`~~ | (legacy) | 凍結；本表查對應 TRK |
| ~~`REG-NNN`~~ | (legacy) | 凍結；本表查對應 TRK |

## 編號分區

為了讓 grep / review 一眼可辨原 namespace，TRK 編號採三段分區：

| 區段 | 來源 | 範例 |
|---|---|---|
| **TRK-001 ~ TRK-099** | `HA-N` 序列 | `HA-11` → `TRK-011` |
| **TRK-100 ~ TRK-199** | `REG-NNN` 序列 | `REG-004` → `TRK-104` |
| **TRK-200 ~ TRK-299** | `TECH-DEBT-NNN` / `TD-NN` 序列 | `TECH-DEBT-005` → `TRK-205`、`TD-022` → `TRK-222` |
| **TRK-300 +** | **post-migration 新分配** | — |

> `TECH-DEBT-NNN` 與 `TD-NN` 是同一個 namespace 的長短形（v2.7.x 之後簡寫為 `TD-`，數字編號連續），同號 alias 合併到同一個 TRK：`TECH-DEBT-022` ≡ `TD-022` → `TRK-222`。
>
> 字母 suffix（e.g. `TD-030a`, `TD-030z`, `TD-032e`）保留，遷移為 `TRK-230a`, `TRK-230z`, `TRK-232e`。

## Mapping 表

### HA-N → TRK-001 ~ 018（DX hardening / automation tracking）

SOT 在 [`dx-tooling-backlog.md`](dx-tooling-backlog.md)。

| Legacy | TRK | 主題 |
|---|---|---|
| HA-1 | TRK-001 | `check_noqa_hygiene.py` noqa/nosec 必要性驗證 |
| HA-2 | TRK-002 | `make test-impact` 變更影響測試自動縮減 |
| HA-3 | TRK-003 | Pre-commit hook CI gate |
| HA-4 | TRK-004 | Lint tool self-test framework（negative fixtures）|
| HA-5 | TRK-005 | `check_test_isolation.py` 測試隔離驗證 |
| HA-6 | TRK-006 | Skip budget CI gate（`make test-skip-audit`）|
| HA-7 | TRK-007 | Lint test coverage 補齊 |
| HA-8 | TRK-008 | CI ignore 文件化與 test-map 更新 |
| HA-9 | TRK-009 | Coverage source 一致性 lint |
| HA-10 | TRK-010 | Flake 自動重試 CI Policy |
| HA-11 | TRK-011 | Fake-Clock 注入（根因修復 Go 時間相依測試）|
| HA-12 | TRK-012 | ADR / 內部連結檔名一致性 Lint |
| HA-13 | TRK-013 | Spoke 文件 Freshness Gate |
| HA-14 | TRK-014 | FUSE-side Git Write 防護 Wrapper |
| HA-15 | TRK-015 | Session 起手式 PATH+PATHEXT Smoke Test |
| HA-16 | TRK-016 | CHANGELOG 計數一致性 Lint |
| HA-17 | TRK-017 | Desktop Commander 長命令 Watchdog Wrapper |
| HA-18 | TRK-018 | `engineering:testing-strategy` Skill 驅動的測試設計還債 |

### REG-NNN → TRK-101 ~ 199（產品 / portal regression registry）

> `known-regressions.md` 於 [Session #16 radical-delete policy phantom-deleted](../CHANGELOG.md)，REG 條目分散於各 PR commit / playbook 引用。

| Legacy | TRK | 主題 |
|---|---|---|
| REG-001 | TRK-101 | (reserved placeholder — 從未實際登錄) |
| REG-003 | TRK-103 | `docs/interactive/changelog.html` 缺 v2.1-v2.6 timeline（v2.8.0 resolved，CHANGELOG.md Phase .a SSOT bundle）|
| REG-004 | TRK-104 | portal-safe hrefs：絕對根路徑 `href="/foo"` 在 portal sub-path 部署會 404；`assertNoAbsoluteRootHrefs` helper 防守 |

### TECH-DEBT-NNN / TD-NNN → TRK-201 ~ 299（platform tech debt）

| Legacy | TRK | 主題 |
|---|---|---|
| TECH-DEBT-001 | TRK-201 | (early-era debt tracking) |
| TECH-DEBT-002 | TRK-202 | (early-era) |
| TECH-DEBT-003 | TRK-203 | (early-era) |
| TECH-DEBT-005 | TRK-205 | palette 殘留導致 dark mode 斷層（ADR-015 cited）|
| TECH-DEBT-006 | TRK-206 | scrollable container axe-core a11y |
| TECH-DEBT-007 | TRK-207 | design-system token canonical 值校正（次要文字 `#475569`）|
| TECH-DEBT-008 | TRK-208 | form element accessible name CRITICAL violation |
| TECH-DEBT-009 | TRK-209 | (dev-rules 縮寫引用 `-009` — early-era) |
| TECH-DEBT-010 | TRK-210 | (early-era) known-regressions registry parser 相關 |
| TECH-DEBT-011 | TRK-211 | (dev-rules 縮寫引用 `-011` — Day 5 runtime axe a11y violation) |
| TECH-DEBT-012 | TRK-212 | (dev-rules 縮寫引用 `-012` — 同上) |
| TECH-DEBT-016 | TRK-216 | MetricCard subStyle 雙背景 dark mode |
| TECH-DEBT-017 | TRK-217 | WatchLoop time.Sleep flake → FakeClock 結構性修復（v2.8.0 PRs #363–#369）|
| TECH-DEBT-018 | TRK-218 | tenant-api async path 測試補洞（[issue #223](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/223)）|
| TECH-DEBT-019 | TRK-219 | tenant-api WebSocket hub housekeeping（含後續可被刪除的 dead-path 標記）|
| TECH-DEBT-020 | TRK-220 | Playwright axe-core a11y spec 從 6 條擴展到 23 條（[issue #225](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/225)；tenant-manager / saved-views 等多個面板的 WCAG 2.1 掃描皆屬此擴展）|
| TECH-DEBT-021 | TRK-221 | `make api-docs` Makefile target / tenant-api swag → OpenAPI spec pipeline（v2.8.0 [#226](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/226)）|
| TECH-DEBT-022 ≡ TD-022 | TRK-222 | schemathesis 契約測試（[issue #231](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/231)）|
| TD-024 | TRK-224 | tenant-api taskmanager / authz 測試以 `pollUntilTerminal` 取代 50ms blind sleep（async terminal-state assertion）|
| TECH-DEBT-026 | TRK-226 | nightly Go race detector `-count=10`（[issue #235](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/235)；`.github/workflows/nightly-race.yaml`）|
| TD-028 ≡ TECH-DEBT-028 | TRK-228 | `/api/v1/me` JSON wire shape nil-vs-empty-array drift（[issue #242](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/242)）|
| TD-029 ≡ TECH-DEBT-029 | TRK-229 | (historic alias slot; 0 current code refs — 保留供舊 PR / commit message 引用對應) |
| TD-030 ≡ TECH-DEBT-030 | TRK-230 | Portal ESM build + Vitest（[issue #247](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/247) Option C sweep）|
| TD-030a | TRK-230a | sub-PR a — foundation |
| TD-030b | TRK-230b | sub-PR b — first wave migration |
| TD-030c | TRK-230c | sub-PR c — `_common/components/` ESM exports（EmptyState、ErrorBoundary 等）|
| TD-030e | TRK-230e | sub-PR e — cicd-setup-wizard fixtures + generators ESM exports（後在 TD-030z 移除）|
| TD-030f | TRK-230f | sub-PR f — `AlertPreviewTab` / `glossary.jsx` ESM imports（jsx-loader `transformImports` 改寫為 window read）|
| TD-030z | TRK-230z | sub-PR z — `jsx-loader.html` 最終下架 |
| TD-031 | TRK-231 | (historic; 0 current code refs — 保留供舊 PR / commit message 引用對應) |
| TD-032 | TRK-232 | Portal E2E coverage push（v2.8.0 LL）|
| TD-032a | TRK-232a | sub-PR a — `check_portal_bundle_size.py` lint（post TD-030 Option C migration）|
| TD-032b | TRK-232b | sub-PR b — cicd-setup-wizard generators property + unit tests |
| TD-032c | TRK-232c | sub-PR c — Alert Noise Analyzer / Alert Simulator E2E smoke specs |
| TD-032d | TRK-232d | sub-PR d — Migration ROI Calculator / Migration Dry-Run Simulator E2E smoke specs |
| TD-032e | TRK-232e | sub-PR e — debug iteration |
| TD-033 | TRK-233 | PR-E rebuild ESM dist regression（chunk-split eval order）|
| TD-034 | TRK-234 | （配對 TRK-233 codify S6 規則）|
| TD-035 | TRK-235 | `skipA11y: true` debt 藏起來，audit 顯示 13/17 多餘 |
| TD-036 | TRK-236 | pre-commit hook（Plan C）擋 `^const \w+\s*=\s*window\.__\w+\s*;` |
| TD-037 | TRK-237 | pre-commit hook S6 — 禁 module-scope `const X = window.__X;` no-fallback reads（hook id `window-x-no-fallback-check`，entry `check_window_x_no_fallback.py`；TRK-236 為前身 draft "Plan C"，已收編於此 hook）|
| TD-038 | TRK-238 | Visual regression baseline 擴張 — Playwright `toHaveScreenshot` 5 staged baselines（Plan A 跨類別覆蓋）|
| TD-039 | TRK-239 | `check_dist_source_consistency.py` + `check_skip_a11y_justification.py`（兩支 lint **docstring 仍寫 `TD-039`** — 它們也是 `tool-map.md` 自動生成的來源；tool-map 因此沿用 `TD-039`。重寫 docstring 會牽動 `check_skip_a11y_justification.py` 的 `RE_JUSTIFICATION = r"//\s*skipA11y:\s*TD-\d+\b"` 正則 + 所有 E2E spec 內已存在的 `// skipA11y: TD-040 ...` 註解；留待 chunk 2b 引入新 lint 時一併處理）|
| TD-040 | TRK-240 | `// skipA11y: TD-040` justification ID（同上，docstring + 正則 + spec 註解整套留待 chunk 2b）|
| TD-042 ≡ TECH-DEBT-042 | TRK-242 | monorepo restructure — portal source 從 `docs/*` 遷至 `tools/portal/*`；`check_dist_source_consistency.py` + `.pre-commit-config.yaml` file-hygiene exclude 也帶有此標 |

### TRK-300+ — post-migration 新分配（無 legacy 對映）

> 此區段**不是 redirect**（無舊 ID 來源），而是 v2.8.1+ 直接以 `TRK-NNN` 新登錄的 tracking entry 索引。backlog 本體（status / owner / pr_ref）在各 issue body + frontmatter；本表給 `TRK ↔ GitHub issue` 快查。

| TRK | Issue | 主題 | Epic |
|---|---|---|---|
| TRK-300 | [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) | AI Tooling Hardening（epic：AI agent 與 Vibe 規則體系交界系統性問題收斂） | — |
| TRK-301 | [#571](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/571) | Plugin prune + CLAUDE.md skill 優先級宣告 | TRK-300 |
| TRK-302 | [#572](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/572) | High-freq feedback 提升進 CLAUDE.md root | TRK-300 |
| TRK-303 | [#573](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/573) | Adversarial self-review 第 6 lens — Mermaid / C4 drift | TRK-300 |
| TRK-304 | [#574](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/574) | Hook/Skill 邊界稽核矩陣（`hook-vs-skill-coverage.md`） | TRK-300 |
| TRK-305 | [#575](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/575) | `vibe-subagent-review` skill — IaC-aware blast radius（complements #448） | TRK-300 |
| TRK-306 | [#576](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/576) | `vibe-release` skill — extends #474 Layer 3 | TRK-300 |
| TRK-307 | [#577](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/577) | `audit_rules_drift.py` + 季度 cron（rule compaction） | TRK-300 |
| TRK-308 | [#578](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/578) | `vibe-brainstorm` skill（deferred → post-ADR-020） | TRK-300 |
| TRK-309 | [#579](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/579) | Upstream skill-system FR tracker（Anthropic / Cowork；backlog，無 milestone） | TRK-300 |
| TRK-310 | [#581](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/581) | epic #570 收尾 — CLAUDE.md 瘦身 + 淨 token 核算 | TRK-300 |
| TRK-311 | [#592](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/592) | Container SAST Layer 1 — Dockerfile（hadolint + Vibe wrapper + .dockerignore） | [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) |
| TRK-312 | [#593](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/593) | Container SAST Layer 2 — Helm template security（kube-linter dual-mode + rationale wrapper） | [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) |
| TRK-313 | [#594](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/594) | Container SAST Layer 3 — Helm values secret-shape lint | [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) |
| TRK-314 | [#595](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/595) | Container SAST Layer 4 stub + CI integration + branch protection | [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) |
| TRK-315 | [#596](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/596) | Container SAST — hybrid-policy codify + consolidated baseline + epic closure | [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) |
| TRK-316 | [#609](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/609) | Tenant Log Query Federation — authz-plane-only, ingestion-decoupled（[ADR-021](../adr/021-tenant-log-query-federation.md) 實作 epic, Phase 1 b → v2.10.0） | — |
| TRK-317 | [#670](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/670) | [Epic] GitOps 寫入平面 resilience hardening（ADR-023，PR [#669](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/669)） | — |
| TRK-318 | [#671](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/671) | WritePR 本地 base stale → 共享檔 silent data loss（鎖內 fetch 方案甲） | TRK-317 |
| TRK-319 | [#672](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/672) | Circuit Breaker GitHub secondary-rate-limit 403 盲點（`isForgeDegradation` 只判 5xx） | TRK-317 |
| TRK-320 | [#673](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/673) | 寫入鎖 load-shedding semaphore + context-aware 取得（消孤兒寫入） | TRK-317 |
| TRK-321 | [#674](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/674) | SSE sync-on-reconnect 前端契約（at-most-once 廣播缺口；Portal track, deferred） | — |
| TRK-322 | [#675](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/675) | SIGTERM 優雅關機 SSE shutdown 廣播（reconnect storm；Portal track, deferred） | — |
| TRK-323 | [#676](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/676) | （wontfix / not planned）special-file 左移驗證 — da-guard CI gate + handler 已覆蓋 | — |
| TRK-324 | [#677](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/677) | 幽靈副本：滾動更新交疊多寫者（Recreate now-fix / Lease deferred） | TRK-317 |
| TRK-325 | [#678](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/678) | 讀寫拆分部署（CQRS）+ read-only enforcement 模式（deferred、**已關閉**；RFC dup [#788](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/788)；re-trigger codify 成 alert `TenantApiReadHANeeded`，見 ADR-023 A4） | TRK-317 |
| TRK-326 | [#751](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/751) | Custom-alert `for` divergence：`for` 納 recipe_id slug + schema enum（向量化靜默覆蓋 P0） | [#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) |
| TRK-327 | [#936](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/936) | Raw-mode forecast 缺現值防抖動地板（anti-flap floor）— defer-with-trigger（Gemini Day-2 review；ratio 模式已有 `_FORECAST_CURRENT_BAND`，raw 模式 out-of-scope at GA） | [#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) |
| TRK-328 | [#964](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/964) | VM parity gate count/time masking — rogue-check 只比 `(alert,direction)` 集合、漏既有分歧惡化成更多 failing assertion（gate-hardening；count-vs-time-vs-tolerance 設計取捨；defer-with-trigger 提議；#958 Gemini ext-review ①） | [#947](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/947) |
| TRK-329 | [#978](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/978) | percent-render 範式守門 — renderer↔fixture 耦合 lint + 慣例 codify（`*100`/`humanizePercentage` 混用防呆、防 8500% 誤渲染；defer-with-trigger；#975 Gemini ext-review 盲區①） | [#947](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/947) |
| TRK-330 | [#985](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/985) | `TenantHAReplicasDegraded` or-LHS masking — 兩臂 `max by(tenant)` label set 相同、SS+Deploy 同時降級只見 StatefulSet（不漏 page、傷 triage 可見度；修法＝`or` 前注入 `workload_kind` label + mutation 回歸案；#982 Gemini ext-review 點 B） | [#875](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/875) |
| TRK-331 | [#916](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/916) | threshold_recommend 覆蓋缺口：下界 metric 語意分流 + observed-map merge-preserve | [#721](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/721) |
| TRK-332 | [#1070](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1070) | SIGKILL 孤兒 `.tmp` 於 PVC 上累積 — boot-time stale-temp GC（defer-with-trigger；#1069 原子替換 review 揪出；emptyDir 部署自癒、PVC 才會累積；trigger＝conf.d 改用 PVC） | [#670](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/670) |
| TRK-333 | [#1092](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1092) | `slo_burn_rate` recipe — 宣告式 SLO burn-rate 告警編譯 feature 線（ADR-031；0-pre custom 子樹 outbound delivery 硬前置 + Phase 0-2 gated） | — |
| TRK-334 | [#1098](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1098) | routes generator tenant 名無 charset 驗證 — parse 期 allowlist fail-loud（matcher 拼接 hardening） | — |
| TRK-335 | [#1127](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1127) | da-portal modal 關閉時未還原鍵盤焦點（WCAG 2.4.3 Focus Order）— 共用 `useModalFocusTrap` return-focus-on-close（**無 issue，直接以 PR 落地**；#1121 P7b review defer 出） | [#962](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/962) |
| TRK-336 | [#1185](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1185) | 補齊本地 gate 覆蓋：CI-only 與單邊 gate 的執行點判準 + 四類確證缺口 | — |
| TRK-337 | [#1189](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1189) | 13 個閾值 key 結構上無法啟用 — `optional_overrides` 到不了 `Defaults`（gate＝`check_threshold_reachability.py`，KNOWN_UNWIRED exit-locked） | — |
| TRK-338 | [#1199](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1199) | ADR-001 severity-dedup 對 kubernetes pack 升級對失效 — 4 對 warning/critical 缺 `metric_group` → 雙發通知（gate＝`check_metric_group_pairs.py`，KNOWN_UNGROUPED exit-locked） | [#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200) |
| TRK-339 | [#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200) | [Epic] Rule-Pack 統整治理 — 契約 SSOT × 可達性 × 閾值體質 × 驗證體質（WS1a registry gate、WS2a 孤兒/配對 gate 等 workstreams） | — |
| TRK-340 | [#1203](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1203) | scrape 面選不到 target：tenant-api ×4 + Alertmanager 守護者告警結構性 inert | [#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200) |
| TRK-341 | [#1204](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1204) | 12 個孤兒 recording rule — series 有值、無人消費（gate＝`check_orphan_recordings.py`，KNOWN_ORPHANS exit-locked；逐條 disposition 追修） | [#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200) |
| TRK-342 | [#1205](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1205) | platform-defaults 繼承路徑 `_custom_alerts` 無深層 schema — 裸 scalar 繞過 #1017 type 鎖 | [#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200) |
| TRK-343 | [#1211](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1211) | custom 子樹 delivery fail-silent ×2：page-mode 無 delivery 零警示＋fragment 模式缺 custom 子樹 | [#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200) |
| TRK-344 | [#1218](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1218) | `init_project` 產生的 `_defaults.yaml` 把 16 個 `*_critical` key 放進 `defaults:` — critical 層只認租戶 override，故該層告警全數不可開火＋每租戶 16 條無人消費的 series | [#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200) |
| TRK-345 | [#1219](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1219) | Defaults Impact Guard 從未檢查觸發它的檔案 — shallow clone 使 `git diff` fatal 被 `\|\| true` 吞掉、scope 靜默退回未改動目錄後貼綠（fail-open） | [#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200) |
| TRK-346 | [#1231](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1231) | `mysql_cpu` → `mysql_threads_running` 底層 key 改名 — owner 終審推翻 #992 ACCEPTED AS-IS（名字非病灶、但雙 series 並發／`_critical` 防線需實修） | [#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200) |
| TRK-347 | [#1233](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1233) | [Epic] ADR-028 federation 撤銷 tamper-evidence 偵測面 revalidation — 偵測鏈路從未端到端驗證（vibe-security-audit re-audit 收斂，5 子項） | — |
| TRK-348 | [#1234](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1234) | 撤銷事件從未進入 VictoriaLogs — Vector 不 tail tenant-api，reconciler 恆對帳空集合、三告警恆綠（主控失效） | [#1233](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1233) |
| TRK-349 | [#1235](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1235) | revoked-set 解析契約：gateway（Lua）與 reconciler（Python）需 byte-exact 一致 + `token_id` charset 驗證（技術細節走私密 advisory） | [#1233](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1233) |
| TRK-350 | [#1236](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1236) | `revoked.txt` missing 時 gateway fail-open 不發 log — `FederationGatewayRevocationLoadFailure` 抓不到 ADR 自己點名的攻擊（文件↔碼矛盾） | [#1233](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1233) |
| TRK-351 | [#1237](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1237) | reconciler 兩支 LogsQL 查詢無來源限定 — 非權威來源可灌入假訊號（alert fatigue → 偵測失能） | [#1233](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1233) |
| TRK-352 | [#1238](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1238) | `FederationRevocationTamperSuspected` `for:5m` 等於 reconcile interval（300s）＋gauge 每輪歸零 → 短窗 un-revoke 結構上不可 page | [#1233](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1233) |
| TRK-353 | [#1250](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1250) | `check_scrape_reachability` 的 `_up` 後綴 heuristic 誤判平台指標 — `channel_up` 拿不到 sibling 的可達性保護（回填：本列於 TRK-354 一併補上） | [#1233](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1233) |
| TRK-354 | [#1269](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1269) | 私密 advisory 的發布觸發點：修法已合入但刻意停在 draft 的決策紀錄＋兩個觸發條件＋發布前檢查（`vibe-release` Rule 4 為其自發火機制） | [#1233](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1233) |
| TRK-355 | [#1273](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1273) | 租戶告警也壓 `alert_source: tenant`，讓平台/租戶過濾兩側都是正向匹配（`defer-with-trigger`；#1270 review defer 出） | — |
| TRK-356 | [#1292](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1292) | 目錄層繼承的 `_custom_alerts` 在 exporter 端沒有資料平面 — Python 編譯器做 UNION 繼承、Go 兩個讀取點只吃 `Tenants[tenant]` ⇒ 整棵子樹的 custom 告警 `on(tenant) group_left` 永遠 join 空集合 | — |
| TRK-357 | [#1322](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1322) | 週期輪詢的 reconciler 抓不到短於一輪（300s）的 un-revoke — TRK-352 的鎖存讓「已取樣到的偵測」抵達人，但不改變是否取樣到；事件驅動偵測需同時放寬三道刻意收緊的權限（`defer-with-trigger`） | [#1238](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1238) |
| TRK-358 | [#1366](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1366) | 行尾政策守衛只是 pytest、不是 pre-commit hook — 忘記 `newline=` 本地全綠、要 push 後才從 CI 得知；建議併進結構相同的 `check_open_encoding.py`（`defer-with-trigger`；#1363 review defer 出） | — |
| TRK-359 | [#1439](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1439) | [ADR-032](../adr/032-paired-interleaved-bench-measurement.md) 實作 — 夜跑效能監測改成對交錯量測（三段：量測管線 → 監測器改寫 → 遷移與驗收）；票內收斂六題待決與三個 ADR 未寫的阻塞點 | — |
| TRK-360 | [#1478](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1478) | 多輪修正收斂協議 —— `vibe-converge` skill + `dev/<scope>/ROUNDS.jsonl` 帳本 + `make converge-status`（decidability gate / 跨輪交接契約 / 面積預算 / 輪數上限 5；⚠️ **2026-08-26 修訂**：`CONVERGED` 已刪除且無替代，見 `references/derivation.md` §4.1／§4.2；由 #1411→#1457 六輪鏈的實測導出：三版述詞對同一個資訊上不可判的問題全掛、每輪插入:刪除 43:1〜53:1、作者自審 0 條）（**無 issue，直接以 PR 落地**） | — |
| TRK-361 | [#1481](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1481) | 跨 AI agent 中性 SSOT — `agents/{skills,roles}/` + `AGENTS.md`（AAIF 標準）成為單一真相源，`.claude/**` 降為 `gen_agent_adapters.py` 的生成物 + drift gate；用複製而非 symlink（Windows host 實測 symlink 壞，#1457）（**無 issue，直接以 PR 落地**） | — |
| TRK-362 | [#1485](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1485) | 吸收 superpowers 兩項缺口能力 —— 完成宣稱鐵律升為 always-on 規則（`AGENTS.md` / `CLAUDE.md`）、收 review 紀律進 `vibe-subagent-review`（take/reframe/reject + 修→回覆→resolve 三件套）；刻意不新增第 9 支 skill（**無 issue，直接以 PR 落地**） | — |
| TRK-369 | [#1593](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1593) | 語言邊界：繁中規範管的是 AI 對人的輸出，repo 工具印給 operator 的字串沿用該工具既有慣例（`dev-rules.md` §9c + `CLAUDE.md` / `AGENTS.md` 措辭對齊）——#1244 的裁決解決了「不要只翻一半」卻沒寫下涵蓋範圍，於是同一條 finding 在每支動到 operator-visible 字串的 PR 上復發（最近一次 #1580）；⚠️ 觸發它的 path instruction 在 CodeRabbit **UI** 側，repo 改不到，故本條不宣稱能單獨終結（**無 issue，直接以 PR 落地**） | — |
| TRK-370 | [#1379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1379) | 客戶文件 ↔ da-tools CLI 契約閘門：產給客戶與教客戶抄的命令，沒有任何機制在驗它們符不符合 da-tools 自己的 argparse 宣告（旗標存在性、預設值、結束碼）。子票 #1380 / #1381 / #1513 / #1619 為內容修正，本條是守衛。⚠️ 本票標題原本掛 `TRK-359`，而那個號碼屬 ADR-032 夜跑對交錯量測（#1439）——本列即為改正 | — |
| TRK-371 | [#1635](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1635) | `bench-baseline.txt` 說不出自己完不完整，所以舊 watchdog 仍會因任何 job 失敗丟掉那一夜（ADR-032 / TRK-359 線；#1626 明白留下的 open gap）。⚠️ **本列是補登**：#1635 開票時把 ID 標為「Proposed，待 maintainer 對 planning SSOT 確認」，而該確認一直沒發生 —— 於是編號只活在 issue body 與交接文件裡，登記表最高停在 TRK-370。⛔ **本列的初稿有一句當場可證偽**：寫的是「#1623 沒有 TRK 編號」，而 #1623 的標題當時就寫著 `TRK-370:`。經盲審查 GitHub API 打掉，改記為下方 TRK-372 那條號碼衝突的處理 | — |
| TRK-372 | [#1623](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1623) | 一個消費者 job 崩掉，會把那一夜的資料從 watchdog 的未來視窗裡刪掉（ADR-032 / TRK-359 線；由 [#1626](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1626) 修一側、其 open gap 由 TRK-371 收尾）。⛔ **本列是一次號碼衝突的裁決紀錄，不只是補登**：#1623 建立於 2026-08-29T12:42Z、標題自掛 `TRK-370`（票內註明「Proposed ID，待 maintainer 對 planning SSOT 確認」，而該確認從未發生）；#1379 則在 **2h22m 後**（15:05Z，PR [#1628](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1628)）由 `TRK-359` 改名為 `TRK-370` 並登記進本表 ⇒ 兩張票同時自稱 TRK-370。⚠️ **裁決讓 #1623 改號而非 #1379，理由是「不讓已 merge 的歷史變假」**：TRK-370 在 #1379 意義下的引用包含**本表**與**已 merge 的 `63d9b03a`**（該 commit 存在的目的就是記錄那次指派）；在 #1623 意義下的引用則全在**未發布的 CHANGELOG `## [Unreleased]`** 內、可直接改。改 #1379 會讓一顆 merged commit 變成假話，改 #1623 不會。⇒ **只改 #1623 的標題，刻意不動它的 body**（GitHub API 取回的 body 帶 HTML entity，原樣寫回會打壞整張票的引號）。**TRK-372 因此已被佔用，新項目自 TRK-373 起** | — |
| TRK-373 | [#1731](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1731) | `bench-probe-write-latency.yaml` 的 Summarize inline python 與 `analyze_probe.py` 是同一份計算的**兩份拷貝**，無任何一致性機制。已知 4 處分歧（校準輪辨識、記錄比對錨定、相關係數最低輪數、NaN 呈現），由 [#1716](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1716) 一輪三位平行盲審找到並逐條重現。⚠️ 修法方向是**去重**不是加差分測試——測試只會把重複鎖死 | — |
| TRK-374 | [#1732](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1732) | `bench-probe-2026-09/README.md` §四 的反事實 harness 從未被 commit、現已不存在 ⇒ 該節數字無法用該目錄的〈重算〉指令重現。房規先例是 `bench-trend-2026-08/counterfactual.py`。**依賴 TRK-373**：兩份拷貝分歧時「驗的是哪一份」才有答案。#1716 只收窄了宣稱，未補回驗證 | — |
| TRK-375 | [#1733](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1733) | probe summary 對格式壞掉的輸入是裸 traceback 而非明確拒收；⛔ 其中一種發生在部分內容已 `tee` 進 `$GITHUB_STEP_SUMMARY` 之後 ⇒ Summary 頁顯示一份印到一半、外觀正常的表，**失敗看起來像成功**。併收跨列 `iters` 不一致不警告、`PROBETAIL` 死碼 | — |
| TRK-376 | [#1734](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1734) | `bench-probe-write-latency.yaml` 的 `PIPESTATUS` 註解技術理由錯誤（pipefail 下 `$?` 已是管線的失敗狀態），且它守的那三行在 `set -e` 下**是死碼**——探針真失敗時自訂的 `::error::` 永遠不會印。既有債，來自已合併的 [#1678](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1678)，不在 #1716 範圍 | — |

## 不在 mapping 範圍

- **`ADR-NNN`** — 架構決策 ID，永不重編號（ADR-019 namespace policy）
- **`S#NNN`** — sprint planning ledger，保留原 namespace（時序語意不同於 TRK）
- **`Trap #N`**（[`windows-mcp-playbook.md`](windows-mcp-playbook.md)）— 環境 trap catalogue，獨立 namespace
- **`pitfall #N`**（[`windows-mcp-playbook.md`](windows-mcp-playbook.md)）— 同上
## 引用慣例（v2.8.1+）

```
✅ 新 entry / commit / PR body / 文件： Resolves TRK-228
✅ 引用歷史記載: 依 source 原文保留（如 ADR-019 內的 TD-NN / HA-NN / REG-NN 字樣若是政策論述引用則照原樣）
⚠️ 過渡期 PR body 寫 `Resolves TD-028` 仍可 work，CI 透過本 mapping 自動翻譯，但會 emit warning「用 TRK-228 取代」
```

## 影響的 lint / 工具

| 工具 | 狀態 |
|---|---|
| ~~`scripts/tools/lint/check_techdebt_drift.py`~~ | **已移除** — `known-regressions.md` 撤除後成 phantom no-op；繼任者 `check_planning_status_sync.py`（chunk 2b，ADR-019 Layer 3）已上線 |
| `scripts/dx/generate_planning_index.py` | chunk 2a 新增，掃 frontmatter 產 `planning-index.md` |
| Pre-commit hooks | 暫不擋舊 ID（過渡期）；chunk 5 收編後正式 deprecate |

## CHANGELOG-archive 與 docs/internal/archive 不動

`CHANGELOG-archive.md`（repo root）+ `docs/internal/archive/` 的歷史敘述**不重寫**——pre-v2.2.0 引用作歷史保留，需要對應 TRK 時來查本表。

## 後續工作

本文件落地（chunk 1）之後：

- chunk 2a — `generate_planning_index.py`（產 `planning-index.md`）
- chunk 3 — 既有 backlog frontmatter migration（一律 TRK-NNN，後續 entries 從 TRK-300+ 分配）
- chunk 2b — `check_planning_status_sync.py` + CI wire（讀 PR body `Resolves TRK-NNN`，驗 frontmatter status）
- chunk 5 — CLAUDE.md 起手式收編 + dev-rules.md / commit-convention.md 強制 TRK

## 關聯

- [ADR-019](../adr/019-planning-ssot.md) — 本 mapping 的政策依據
- [issue #379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) — 本 mapping 是 chunk 1 deliverable
- [dev-rules.md §P1](dev-rules.md) — commit trailer 規範
- [dx-tooling-backlog.md](dx-tooling-backlog.md) — TRK-001~018 的 source
