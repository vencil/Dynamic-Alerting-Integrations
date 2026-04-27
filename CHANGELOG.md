---
title: "Changelog"
tags: [changelog, releases]
audience: [all]
version: v2.7.0
lang: zh
---
# Changelog

All notable changes to the **Dynamic Alerting Integrations** project will be documented in this file.

## [Unreleased]

<!-- Editorial guideline（v2.8.0, 建立於 2026-04-23, PR #50）：

本節為 v2.8.0 in-progress 工作暫存區；**entries 目標長度：每筆 3-6 行面向
使用者的重點 + 一行 `詳見 planning §N` / `commit <sha>` 指回內部 artifacts**。
不要在此處記錄 session 過程、FUSE trap 實測、Cowork day-by-day、完整 commit
list、每個 hook 名單等——該類內容屬於：

  - docs/internal/v2.8.0-planning.md §12 Session Ledger / Live Tracker
  - docs/internal/v2.8.0-planning-archive.md RCA sections
  - commit messages / PR discussion

Phase .e E-5 會做最終 condensation + 切正式 `## [v2.8.0]` heading；但若每筆
bundle entry 都 ~30 行敘事，E-5 會變成重寫而非潤飾。請自律。

Compare：v2.7.0 最終條目約 55 行（Scale / Token / Test / Benchmark / ADR /
Breaking / Upgrade 七塊清楚區分），那是目標形狀。
-->

### Security

- **Grafana 12.4.1 → 12.4.2（v2.8.0, [#98](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/98) of umbrella [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100) Q2 2026 CVE audit）** — bump `k8s/03-monitoring/deployment-grafana.yaml`。**audit prerequisite confirmed**：`configmap-grafana.yaml` 唯一 data source 是 Prometheus，**無 PostgreSQL**，故 12.4.1 帶的 pgx/v5 CVE-2026-33816（CRITICAL memory-safety, fixed in 5.9.0）在我們部署是 dead-code。bump 主要修 12.4.1 → 12.4.2 自身的 Go binary CVE（含 grpc CVE-2026-33186 transitive）。Trivy 0.70.0 audit：Go binary 部分 8/2 → 7/1（pgx 那條 CRITICAL 即使可達也已被 12.4.2 fix）。**殘留 11 HIGH + 2 CRITICAL Alpine OS layer**（OpenSSL/libssl3/musl/zlib）upstream Grafana 還沒 rebase Alpine 3.22+，CRITICAL CVE-2026-31789 是 32-bit only（amd64 N/A），HIGH OpenSSL CMS path 在 Grafana 不啟用 — 留 issue 追 upstream rebase。詳見 [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100) audit 摘要。

- **Prometheus + Alertmanager stack bump（v2.8.0, [#96](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/96) + [#97](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/97) of umbrella [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100) Q2 2026 CVE audit）** — bump 4 處 image refs 對齊 prom/* monitoring stack：`k8s/03-monitoring/deployment-prometheus.yaml` v3.10.0 → **v3.11.2**、`k8s/03-monitoring/deployment-alertmanager.yaml` v0.31.1 → **v0.32.0**、`tests/e2e-bench/docker-compose.yml` 的 prometheus v2.55.0 → v3.11.2（**Prometheus 2.x 已 EOL 2024-12-03**）+ alertmanager v0.27.0 → v0.32.0。Trivy 0.70.0 audit 2026-04-26：清掉 grpc CVE-2026-33186（CRITICAL memory-safety）+ Go stdlib 1.25.7 / 1.26.0 → 1.26.2 升級涵蓋的全部 CVE-2026-32280/281/283 等 + e2e-bench 那組 v2.55.0 帶的 11 個 stdlib CVE（Go 1.23.2 → 1.26.2）+ x/crypto CVE-2024-45337（CRITICAL SSH misuse, fixed in v0.31.0）。Prometheus k8s 9 HIGH + 1 CRITICAL → 3 HIGH + 0 CRITICAL（殘留 jsonparser/moby authz/otel BSD-only，皆不可達）；Alertmanager k8s 6 HIGH + 1 CRITICAL → 1 HIGH + 0 CRITICAL（殘留 otel-go kenv path 是 BSD-only）。e2e-bench compose 改 v3.x 後 18 HIGH + 3 CRITICAL → 3 HIGH + 0 CRITICAL（同 k8s 殘留）。**注意**：Prometheus 2.x → 3.x 是 major bump，但本 PR 用到的 flags（`--config.file`、`--web.enable-lifecycle`、`--storage.tsdb.retention.time`）都仍向下相容；現有 `prometheus.yml` / `alert-rules.yml` 格式無 breaking change。詳見 [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100) audit 摘要。

- **oauth2-proxy v7.7.1/v7.15.1 → v7.15.2（v2.8.0, [#92](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/92) of umbrella [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100) Q2 2026 CVE audit）** — bump 5 個 image references 對齊到單一版本：`k8s/04-tenant-api/deployment.yaml`（從 v7.7.1 跳兩個 minor）、`helm/da-portal/values{,-tier1,-tier2}.yaml` 與 `helm/tenant-api/values.yaml`（從 v7.15.1）。clear 三條 auth bypass：CVE-2026-34457（CRITICAL：health-check User-Agent matching 繞 `auth_request` mode）、CVE-2026-40575（CRITICAL：`X-Forwarded-Uri` header spoofing）、GHSA-pxq7-h93f-9jrg（HIGH：fragment confusion in `skip_auth_routes`）。Trivy 0.70.0 audit 2026-04-26：`v7.7.1` 帶 16 HIGH + 6 CRITICAL（含早期 CVE-2025-54576 fixed in 7.11.0）、`v7.15.1` 帶 6 HIGH + 2 CRITICAL；target `v7.15.2` 在 trivy 上 **0 / 0 clean**（Debian 13.4 + go binary 全乾淨）。修掉 v7.7.1 vs v7.15.1 的 inter-file drift。詳見 [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100) audit 摘要。

- **alpine/git init container 2.43.0 → v2.52.0（v2.8.0, [#93](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/93) of umbrella [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100) Q2 2026 CVE audit）** — bump tenant-api init container 在 `k8s/04-tenant-api/deployment.yaml` + `helm/tenant-api/templates/deployment.yaml`。clear 8 個 git binary CVE 含 4 個 RCE/exfil class：CVE-2024-32002（CRITICAL recursive-clone RCE）、CVE-2024-32004（local clone RCE）、CVE-2024-32021（symlink bypass）、CVE-2024-32465（local RCE）、CVE-2024-52006（credential exfil via newline confusion）、CVE-2025-46334、CVE-2025-48384（RCE）、CVE-2025-48385（arbitrary file write）。Trivy 0.70.0 audit：current 32 HIGH + 4 CRITICAL → target 13 HIGH + 2 CRITICAL（殘留全是 Alpine 3.23.3 OS layer，但 init container 只 clone 受信任內網 GitOps repo，OpenSSL DoS / CMS 殘留 CVE 不可達）。詳見 [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100) audit 摘要。

- **Python base image 3.13.3 → 3.13.13-alpine3.22（v2.8.0, [#94](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/94) of umbrella [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100) Q2 2026 CVE audit）** — bump `components/da-tools/app/Dockerfile`（builder + runtime）+ `tests/e2e-bench/driver/Dockerfile`（順手把 floating `python:3.13-alpine` 釘到 explicit version 取得 reproducibility）。Trivy 0.70.0 audit 2026-04-26：current `python:3.13.3-alpine3.22` 帶 15 HIGH + 5 CRITICAL（含 CVE-2025-15467 OpenSSL pre-auth RCE、CVE-2025-6965 sqlite integer truncation、CVE-2025-48174/48175 libavif、CVE-2026-31789 OpenSSL heap overflow on 32-bit）；target image 在 trivy 上 **0 / 0 clean**（Alpine 3.22.4 patched + Python 3.13.13 stdlib fixes）。da-tools Dockerfile 既有的 `RUN apk --no-cache upgrade` 仍保留作 defense-in-depth。詳見 [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100) audit 摘要。

- **Go toolchain 1.26.1 → 1.26.2（v2.8.0, [#95](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/95) of umbrella [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100) Q2 2026 CVE audit）** — bump 三份 `go.mod` (`components/tenant-api`、`components/threshold-exporter/app`、`tests/e2e-bench/receiver`) + 對應 3 份 Dockerfile 從 floating `golang:1.26-alpine` 釘到 `golang:1.26.2-alpine3.22`。一次清掉 10 個 stdlib CVE：CVE-2026-33810（Go 1.26 only x509 wildcard bypass）、CVE-2026-27140（cmd/go SWIG cgo trust-layer bypass，build-time RCE）、CVE-2026-32289（html/template XSS）、CVE-2026-32282（os.Root.Chmod symlink traversal）、CVE-2026-32283（TLS 1.3 key-update DoS）、CVE-2026-32280/32281（crypto/x509 DoS）、CVE-2026-32288（archive/tar unbounded allocation）、CVE-2026-27143/27144（cmd/compile memory corruption + type confusion）。Trivy 0.70.0 audit 2026-04-26 confirm。詳見 [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100) audit 摘要。

### Fixed

- **CLAUDE.md L57 stale ref to `doc-map.md § Change Impact Matrix`（[#66](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/66) part 2）** — 該章節從未存在於 `doc-map.md`（auto-generated catalog，無 manual section）。移除 parenthetical 連結；保留主規範文字「影響 API / schema / CLI / 計數的變更須同步 `CHANGELOG.md` + `CLAUDE.md` + `README.md`」自身已完整。`#66` 另一半（tool-count drift 121 vs actual 122）已於 PR #71 一併修。`#66` 可 close

### Changed

- **doc-map scope 收窄為公開文件，`docs/internal/**` 不再 catalog（v2.8.0, [#66](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/66) follow-up）** — `generate_doc_map.py` `SKIP_DIRS` + `_version_patterns.py` `DOC_MAP_SKIP_DIRS` 同步加入 `internal`。理由：catalog 真正的 consumer 是「快速查找公開文件」（AI agent / 開發者 discovery）；internal docs 的 discovery path 是 CLAUDE.md / skills / `vibe-playbook-nav`，不需要 catalog 重複登錄。修掉這個 scope 不一致也順手清掉 `validate_docs_versions.py` 對 `v2.8.0-{planning-archive,tech-debt-decomposition}.md` 的 false-positive `doc-map-coverage` warning（產生器故意排除但驗證器不知道）。doc-map 從 134 → 114 entries；公開文件 count 在 CLAUDE.md L83 + README.md 對應行同步更新並改用「公開文件」措辭以反映 scope 收窄。catalog 自描述的 `doc-map.md` / `tool-map.md` meta-entries 仍由 `SELF_ENTRIES` 手動加入，不受影響

- **GitHub Actions Node 20 → Node 24 sweep（v2.8.0, chore）** — 升級全部 11 個 workflow 用到的 7 個 actions 到當前最新 stable major，避開 2026-06-02 GitHub 強制 Node 24 deadline + 26 週後 Node 20 移除：`actions/checkout@v4 → @v6`（11 處）、`actions/setup-python@v5 → @v6`（6 處）、`actions/setup-go@v5 → @v6`（3 處）、`actions/setup-node@v4 → @v6`（3 處，**確認** repo 未用 yarn/pnpm cache 故 v6「limit auto-cache to npm」breaking 不影響）、`actions/cache@v4 → @v5`、`actions/upload-artifact@v4 → @v7`、`actions/github-script@v7 → @v9`（**確認** repo 3 個 inline script 全使用 `require('fs')` + injected `github.rest.*`，未踩到 v9 移除的 `require('@actions/github')` 路徑）。74 個 reference 全替換；diff 嚴格只動 version tag 不動其他語意。verification path：本 PR 自身 CI run（trigger 多數 PR-level workflow）+ post-merge `gh workflow run bench-record.yaml`、`mkdocs-deploy.yaml` 手動 dispatch（cron/push-only workflow）

### Added

- **Phase .b — Track A bundle: Phase B closure-plan punch list 10 items（v2.8.0, route C strict-plan adherence kickoff）** — 統一收 v2.8.0 Phase B 深度 review 後的 P0/P1/P2 punch list，全部不依賴 customer-anon sample 的修補一次落地。零 production behavior change（除 A4 logging level + metric counter），全部由 unit tests 守關。
  - **A1 — `config_slow_write_stress_test.go` 註解 50ms→100ms** — file header 說 "50ms window" 與 line 64 實際 `debounceWin=100*time.Millisecond` drift；統一為 100ms（rationale 段本來就寫對）。零行為變動
  - **A2 — `_gen_defaults_yaml` numeric-only contract regression test** — cycle-6 RCA (PR #105) 的 root-cause fix 沒有 test 守關。新增 2 cases (full-default + db_types-subset) 斷言 defaults block 全 `int|float`（拒絕 bool）。任何 future PR 把 scheduled-form metric 加進 METRIC_TEMPLATES + 重用 generator 會 fail-fast，不再默默把 e2e harness 拖進另一輪 6-cycle saga
  - **A3 — `bench-e2e-record.yaml` log capture 加 pushgateway** — cycle-3 cause #2 (driver pushed before pushgateway listened) 的最高訊息密度 signal 在 pushgateway log；既有 5 services dump 漏這支。1-行 fix
  - **A4 — `_defaults.yaml` parse failure WARN→ERROR + emit `da_config_parse_failure_total`（cycle-6 RCA 守關，flat-mode + hierarchical-mode 雙路徑）** — 兩條獨立路徑都 codify：(i) flat-mode 三個 yaml.Unmarshal 失敗點 (loadDir / IncrementalLoad / fullDirLoad in `config.go`) 對 `_*` 前綴檔升 ERROR level 並 increment 既有 A-8d metric；(ii) **hierarchical-mode 生產熱路徑** (`recomputeMergedHash` → `computeMergedHash` in `config_debounce.go`) 新 helper `emitParseFailureSignal` 偵測 defaults-chain parse failure → ERROR log + per-tenant IncParseFailure（per-tenant 重複是特性，counter 即 blast-radius signal）。理由：cycle-6 燒 ~5 hours wall-clock 因為「skip unparseable file」WARN 在 `gh run view --log` 太容易錯過；hierarchical 是 production 真正的 hot path（per archive S#27 finding 3）。兩條新 invariant tests：`TestConfigManager_LoadDir_UnparseableDefaultsErrorAndMetric` (flat) + `TestRecomputeMergedHash_DefaultsParseFailureEmitsErrorAndMetric` (hierarchical)；二者都跑過 invariant flip 驗證（暫時把 production code 的 ERROR 改 WARN，二者都正確 fail）
  - **A5 — `da-tools tenant-verify` 新 CLI subcommand**（B-4 rollback checklist item 6 解 dead-link）— `scripts/tools/dx/tenant_verify.py` ~150 LoC + 12 unit tests + da-tools entrypoint registration + Dockerfile bundle (含 transitive `describe_tenant.py`). Exit 0/2 contract 對應 B-4 §「驗證 checklist」第 6 項「checksum 必須回到 Base PR merge 前的 `merged_hash`」；`--all --json` mode 給 ops 拍 pre-base snapshot 與 rollback 後 diff。Migration playbook ZH+EN 雙語對齊 update 把舊文裡虛指的 `da-tools tenant verify <id>` / `da-tools effective <id> --diff-against-sha` 都改寫為實際可跑指令
  - **A6 — `generate_tenant_fixture.py --extra-defaults KEY=NUMERIC` flag + 新 helper `scripts/ops/inject_default_key.py`** — bench harness 之前用 inline-Python 在 `bench_e2e_run.sh` 寫 `bench_trigger=50` 進 `_defaults.yaml`（cycle-3 workaround）。雙路徑 codify：(i) generator path 走 `--extra-defaults`，`_gen_defaults_yaml` 接受 `extra_defaults` dict 並驗 numeric；(ii) non-generator path（customer-anon / 預先生成 fixture）走新 helper script，**取代** orchestrator 內 ~30 行 inline-Python，shell 端只剩 `python3 inject_default_key.py "$DEFAULTS_FILE" bench_trigger 50` 一行。Helper idempotent（key 已在則 no-op）+ 拒非數值 + 三態 file state（exists+key / exists+no-key / missing）。13 new tests（generator 3 + helper 10）
  - **A7 — `aggregate.py` Stage C ≤ 0 → null + `stage_c_note`** — 5000-tenant 觀察 artifact (T3 lands before T2，Stage C 算成 0) 之前直接以 0 進 schema 誤導 reviewer。改：Stage C n/p50/p95/p99 只算 positive 樣本，全 ≤0 emit `stage_c_note: "absorbed_into_AB"`，部分 ≤0 emit `absorbed_into_AB_partial:N/M`（schema 在 docstring 鎖死為 regex `r"absorbed_into_AB_partial:(\d+)/(\d+)"`，N<M 嚴格）。Stage A/B/D 行為不變。5 new tests（3 行為 + 2 schema lock）
  - **A8 — Tier 1 fail-fast smoke gate 擴 resolve phase** — 既有 gate 只檢 fire phase 5 anchors，但 Alertmanager `send_resolved: false` 若被未來 commit 誤入會讓 resolve 路徑壞（**只壞 T4，不壞 T3** — Prometheus 內部 state machine 仍 resolve）而 fire 仍綠。新 `RESOLVE_REQUIRED_ANCHORS = (T0, T3, T4)`（T1/T2 在 `stage_ab_skipped: True` 路徑跳過合理）；anchor 名稱以 `fire.` / `resolve.` 前綴回傳讓 operator 一眼看出哪 phase 壞。同步更新 `docs/internal/benchmark-playbook.md` §「對應信號」表格成 `fire.<key>` / `resolve.<key>` 命名 + 加 resolve.* signal 條目（`send_resolved: false` → resolve.T4 是常見原因）。10 既有 test 改寫 + 5 新 test（含 T4-only signature）
  - **A9 — `docs/benchmarks.md` §12 加 Phase 2 e2e 章節 visibility note（ZH + EN）** — 公開 perf doc 之前完全沒提 `tests/e2e-bench/` harness 存在；customer-anon sample 抵達前刻意不放數字 (per pitch-deck rationale)，但需要把「框架已就位、待校準後升格」的訊號 surface 出來。Cross-link `internal/benchmark-playbook.md#v280-phase-2-e2e-alert-fire-through-b-1-phase-2`
  - **A10 — `dev-rules.md §P3` 高成本 workflow_dispatch 需明確 user 授權** — codify S#45 archive permission lesson：cycle-6 後 agent 自跑 5000-tenant 被 runtime 擋的事件升為文字規範。Perimeter 只圈當前確認的 cost-class workflows (`bench-e2e-record.yaml` / `bench-record.yaml`)，未來新增直接改 code-driven 偵測，不改本條
  - **A5 design tradeoff**：`describe_tenant.py` 隨 `tenant_verify.py` ship 進 da-tools docker image 但**不註冊**為 `da-tools describe-tenant ...` 公開 subcommand — 它仍是 v2.7.0 internal tool 暫不承諾 stable CLI surface，供 `tenant_verify` 作 transitive lib import 用。Image 內 `python /app/describe_tenant.py ...` 仍可跑（檔案存在），未來公開化走獨立 entrypoint.py PR
  - **Test sweep**：91 Python tests pass (`tests/dx/test_generate_tenant_fixture.py` 15 + `tests/dx/test_tenant_verify.py` 12 + `tests/ops/test_inject_default_key.py` 10 + `tests/e2e-bench/test_aggregate.py` 29 + `tests/e2e-bench/driver/test_driver.py` 25) + Go pass on touched paths（含 2 new parse-failure invariant tests，flat + hierarchical，皆通過 production-code-flip 驗證非 vacuous-pass）
  - **不在本 bundle scope（route C 後續 track 處理）**：B-2 SLO definitive sign-off (Track D)、B-5 Mixed-mode 驗證 (Track B)、B-6 Tenant API hardening (Track C)、Track D staging 7-day observation、Track E customer-anon sample。詳見 maintainer-local closure plan

- **Phase .c — C-11 Migration Toolkit packaging（v2.8.0）** — 把 customer migration 工具集（da-tools Python CLI + da-guard Go binary）打包成三條交付路徑，讓客戶從 ghcr.io / GitHub Release / air-gapped tar 任選其一安裝：
  - **Path A — Docker pull from `ghcr.io/vencil/da-tools:v<X.Y.Z>`**：擴 `components/da-tools/app/Dockerfile` bundling da-guard linux/amd64 binary 進 `/usr/local/bin/da-guard`，讓 `da-tools guard` 子命令在 image 內就能 work（不需 customer 另設 `$DA_GUARD_BINARY` 或外部 install）。`build.sh` 在 `--assemble-only` 階段自動 `go build` da-guard binary 進 build context；本地 dev 跑 `bash build.sh` 也會跑這步（缺 Go 直接 fail 而非 silent skip）
  - **Path B — Static binary download**：每個 `tools/v*` Release 自動 attach 6 個 cross-compiled da-guard binaries（linux/darwin/windows × amd64/arm64），加單一 `SHA256SUMS` 檔 list 全部 archive + raw binary 的 hash。Linux/macOS tar.gz、Windows zip
  - **Path C — Air-gapped tar import**：`docker save | gzip` 產出 `da-tools-image-v<X.Y.Z>.tar.gz` + `.sha256`，attach 到同一個 Release。客戶在 isolated 環境 `gunzip -c | docker load` 即可
  - **Workflow 擴充** `.github/workflows/release.yaml::release-da-tools` 加：(i) **VERSION-vs-tag 一致性檢查**（`components/da-tools/app/VERSION` 必須等於 `tools/v<X>` tag 中的 X，否則 fail）；(ii) `actions/setup-go@v6` + cross-compile 6-arch matrix；(iii) 自動 tarball/zip + SHA256SUMS 產生；(iv) `docker save | gzip` air-gapped tar；(v) 自動 `gh release create --generate-notes`（idempotent — 若 maintainer 預先建 Release 就改 `gh release upload --clobber`）。Trivy CVE scan 仍跑、Helm chart push 仍跑
  - **Customer-facing 安裝指南** `docs/migration-toolkit-installation.md` (zh + en)：3 條路徑安裝指令、hash verification、升級流程、6 個常見故障排除。`migration-guide.md` 與 `migration-guide.en.md` 都加 cross-ref；公開文件數 115 → 116（CLAUDE.md + README.md 計數同步）
  - **Honest scope discipline (deferred to PR-3)**：cosign / GPG 簽章（DEC-J 待客戶 security team 提需求才啟動）、SBOM 自動生成（Trivy 已附 vuln scan，full SBOM 是 nice-to-have）、Windows 簽章 (Authenticode)
  - **PR-2 v1 only ships `da-guard`**：planning §C-11 願景包含 C-8 parser CLI / C-10 batch-pr CLI / C-9 profile-builder CLI 一起進 toolkit；那些 CLIs 還沒 ship（C-8 PR-2 + C-10 PR-5 + C-9 PR-4 範疇）。本 PR 把 release infra 建好，未來 CLI binary 加入時 cross-compile matrix 直接擴展即可
  - 詳見 `docs/migration-toolkit-installation.md` / planning §12.2 Phase .c row C-11

- **Phase .c — C-10 Batch PR Pipeline PR-2 — apply mode (push branches + open PRs via GitHub)（v2.8.0）** — turns C-10 PR-1's planner output into actual git branches + GitHub PRs. Now the C-9 PR-3 conf.d-shape emission can land in real customer repos as Draft PRs grouped by Hierarchy-Aware chunking (Base Infrastructure PR + per-domain tenant chunk PRs):
  - **`internal/batchpr/apply.go::Apply()`** — pure orchestration walking `Plan.Items` in order. Per-item pipeline: deterministic-branch-name → idempotency check (skip if branch+open-PR already exist) → CreateBranch → WriteFiles → Commit → Push → OpenPR. Per-step failure becomes `ApplyStatusFailed` with descriptive error message; subsequent items still attempted (one bad tenant doesn't sink the batch). Context-cancellation aware. Inter-item delay (configurable, default 0) for GitHub secondary rate-limit softening on 50+ tenant batches
  - **`<base>` placeholder rewrite** — PR-1's tenant PR descriptions carry `Blocked by: <base>` placeholder text; Apply() substitutes the actual Base PR number after the Base PR opens (`PRClient.UpdatePRDescription`). When the Base PR fails to open, tenant PRs still create with the literal placeholder + a clear warning so reviewers know to manually edit the cross-reference
  - **Idempotency via deterministic branch hashing** — branch name = `<prefix>/(base|tenant-<chunk-key>)-<plan-hash>` where plan-hash is SHA-256[:8] of `{Kind, SourceProposalIndices, ChunkKey, TenantIDs}` per item (Title / Description excluded so rendering drift doesn't change the hash). Re-running Apply() against the same Plan + remote sees existing branches and records `ApplyStatusSkippedExisting` rather than duplicating PRs. Customer-renamed `BranchPrefix` honored
  - **Interface split — IO at the edges** — `GitClient` (CreateBranch / WriteFiles / Commit / Push / BranchExistsRemote) and `PRClient` (OpenPR / FindPRByBranch / UpdatePRDescription) interfaces decouple orchestration from side effects. Tests use in-memory stubs (no disk + no network); production wires shell-out impls (`ShellGitClient` calls `git`; `GHPRClient` calls `gh`). Customer can swap to go-git / go-github / GitLab impls without touching apply.go
  - **Production impls match repo convention** — `git_shell.go` mirrors `tenant-api/internal/gitops/writer.go`'s shell-out style (one process boundary, no go-git heavy dep). `pr_gh.go` matches `.github/workflows/release-attach-bench-baseline.yaml` + `.github/workflows/guard-defaults-impact.yml` which both shell out to `gh` (consistent customer auth surface; GH_TOKEN / `gh auth status` already familiar)
  - **`AllocateFiles(plan, files)` helper** — splits the C-9 emit `path → bytes` map into per-PlanItem buckets following ADR-019 §1: `_defaults.yaml` + `PROPOSAL.md` → Base PR, `<tenant-id>.yaml` → matching tenant chunk PR, anything else → warning. Caller (CLI / UI) calls AllocateFiles between Emit and Apply
  - **DryRun honoured** — `ApplyInput.DryRun=true` runs the full orchestration loop with `ApplyStatusDryRun` per item, no git or API calls. Useful in CI dry-run flows + customer pre-flight reviews
  - **30+ new tests `-race -count=2` 穩定** — happy path / DryRun / idempotency (existing branch + existing PR) / EmptyFiles skip / per-step failure (6 pipeline points) / context cancellation mid-loop / `<base>` placeholder rewrite happy + skipped-when-base-fails / custom BranchPrefix / hash determinism + structural-change-flips / Title-drift-doesn't-flip / safeBranchSegment edge cases / AllocateFiles 6 cases (happy / empty plan / empty files / unknown tenant warns / unrecognised file shape / no-Base-PR / dup-tenant-goes-to-first) / pr_gh URL parsing + arg construction. tenant-api full suite + threshold-exporter full sweep also clean
  - **PR-2 scope discipline** — does NOT do: rebase open tenant PRs onto merged Base (PR-3 `refresh --base-merged` territory) / data-layer hot-fix `refresh --source-rule-ids` (PR-4) / CLI subcommand wiring (`da-tools batch-pr {plan,apply,refresh}` is PR-5)
  - 詳見 `internal/batchpr/apply.go` package header / planning §12.2 Phase .c row C-10

- **Phase .c — C-9 Profile Builder PR-3 — PromRule→threshold translator + ADR-019（v2.8.0）** — closes the C-9 emission gap that PR-2 honestly punted: PR-2 shipped intermediate-format artifacts (`shared_expr_template`, `dialect`, etc.) but the threshold-exporter runtime needs `defaults: {<metric_key>: <numeric>}` shape. PR-3 adds the bridge:
  - **`internal/profile/translate.go`** — pure Go translator. `TranslateRule(ParsedRule) → RuleTranslation` walks the metricsql AST to find a top-level numeric comparison (`>`, `>=`, `<`, `<=`), pulls out the threshold scalar, and resolves a `metric_key` via the 5-step ladder pinned in ADR-019: explicit `metric_key` label → alert name snake_case → record name snake_case → inner metric name → skipped. Inverted forms (`0.85 < x`) auto-flip to canonical `metric op threshold`. Equality operators (`==`/`!=`) are an explicit non-goal — surface as `TranslationSkipped` with a clear reason
  - **`TranslateProposal(prop, members, tenantKey)` cluster aggregator** — applies per-rule translation, picks majority for `metric_key` / `operator` / `severity`, takes the **median** of member thresholds for `default_threshold` (resists outliers — single tenant with 5000 won't pull a 80→1700 mean disaster). Per-tenant overrides only emitted for members whose value diverges from cluster default. `Status==TranslationOK` requires all axes unanimous + every member translated cleanly; otherwise downgrades to `Partial` with dissent warnings written into `_defaults.yaml` header comment + `PROPOSAL.md`. Cluster with zero translatable members → `Skipped` → caller falls back to PR-2 intermediate emission
  - **`emit.go::EmissionInput.Translate` flag** — caller opts in. `false` (default) preserves PR-2 backwards-compat. `true` enables per-proposal dispatch: TranslationOK/Partial → conf.d-shape (`defaults:` + per-tenant `tenants:` blocks), TranslationSkipped → intermediate format. Per-proposal granularity means a mixed easy/hard customer corpus still gets maximum value — translatable clusters land conf.d-ready, others surface for human review without sinking the batch
  - **Conf.d-shape emission details**：`_defaults.yaml` carries `defaults: {<metric_key>: <numeric>}` with a header comment block (provenance + dialect + translation status + warnings) so reviewers see soft spots inline. Per-tenant `<id>.yaml` carries `tenants: {<id>: {<metric_key>: "<value>"}}` with the threshold quoted as a string (matches `config_resolve.go::ResolveAt`'s `value:severity` parser; severity-tier translation is an explicit non-goal of PR-3). Tenants matching cluster default get NO override file — the GitOps line-savings ADR-019 §1 promises
  - **ADR-019 (slim) — Profile-as-Directory-Default** — `docs/adr/019-profile-as-directory-default.md` (zh + `.en.md`) 釘死跨組件「default vs override 邊界」原則：cluster median in `_defaults.yaml`；只有偏離 default 的 tenant 寫 `<id>.yaml` override。影響 C-9 emission / C-10 directory placement / C-12 guard / C-11 packaging 共四處的一致性。Cross-component non-goals 列明：directory inference 延 C-10 PR-3 / dim+regex labels emission / source PromRule auto-rewrite / two-tier severity。**Translator 演算法細節（metric_key 5-step ladder、median、cluster aggregation、operator handling、status fallback）寫在 `internal/profile/translate.go` package header**，避免 ADR + code doc 雙寫漂移。ADR README index + cross-ref to ADR-017/018 同步
  - **20+ new tests / `-race -count=2` 穩定**：`translate_test.go` 18 cases (explicit-label happy / alert snake_case fallback / record fallback / inverted operator flip / all 4 ops × 2 sides / no-comparison skip / vector-comparison skip / parse-error skip / equality non-goal skip / empty-expr error / metric-key precedence / snake-case edge cases incl. acronym→Word boundary like `MySQLHigh→my_sql_high`) + 8 cluster cases (happy + per-tenant override divergence / metric_key dissent majority / median-vs-outlier / all-skipped / partial / vote tie-break / median odd-even / formatVotes determinism). Plus 5 new emit cases for the translated path (conf.d-shape happy / PROPOSAL.md translation summary / fall-back on skip / yaml round-trip valid / formatThresholdString)
  - **Honest scope discipline** — PR-3 doesn't auto-rewrite PromRule expressions, doesn't translate dimensional / regex labels, doesn't do histogram quantile bucketing, doesn't do two-tier severity; all listed in ADR-019 §non-goals with deferral plan
  - 詳見 ADR-019 / `internal/profile/translate.go` package header / planning §12.2 Phase .c row C-9

- **Phase .c — C-12 Dangling Defaults Guard PR-5 — GitHub Actions wrapper + redundant-override warn 啟用（v2.8.0）** — 把 PR-4 ship 的 `da-guard` CLI 包成 PR-time gate：
  - **新 workflow** `.github/workflows/guard-defaults-impact.yml`：`pull_request` 觸發於 `**/_defaults.yaml` 變更（plus self-PR-of-the-workflow / da-guard source paths），build da-guard binary → run guard → post sticky PR comment（marker `<!-- da-guard-defaults-impact -->`，每次 push update-in-place）。Pattern 直接借用 `blast-radius.yml`。Workflow 也支援 `workflow_dispatch` + `config_dir` input 手動跑、artifact 上傳保 14 天 (`da-guard-report`)。Exit 1/2 都 fail workflow 擋 merge；exit 0 (clean 或只有 warnings) 通過
  - **Scope 推算**：偵測 PR 變更的 `_defaults.yaml` 數，**單一變更**時 narrow scope 到該檔 dirname；**多檔變更**時 fall back 整棵 conf.d/。後者依賴 PR-5 啟用的 cascading 正確性（見下）。Conf.d 路徑解析三層：`workflow_dispatch.config_dir` input → repo-root `conf.d/` → `components/threshold-exporter/config/conf.d/`
  - **Redundant-override warn 啟用 + cascading 正確性**：PR-4 留下 `TenantOverrides` + `NewDefaults` 兩個 guard input nil-未填，PR-5 把這條 wire 接通，並且**為 cascading L0/L1/L2 場景設計新欄位**：
    - `pkg/config.EffectiveConfig` 加 `TenantOverridesRaw map[string]any` + `MergedDefaults map[string]any`（兩者皆 `json:"-"`，**不**進 tenant-api `/effective` JSON 回應，guard-only）
    - `computeEffectiveConfigBytesDetailed()` 新 helper（`computeEffectiveConfigBytes` 改為 thin shim）—— 在 deepMerge tenant override **之前** 取 `deepCopyMapH(merged)` snapshot，避免後續 merge 透過 alias 污染 snapshot。Defends against shared sub-map mutation
    - `internal/guard.CheckInput` 加 `NewDefaultsByTenant map[string]map[string]any`：per-tenant merged-defaults map（PR-1 的 single `NewDefaults` 留作 fallback，preserves backwards-compat）。Resolution rule：per-tenant entry 存在則用之、否則 fall back global、兩者皆無則 silent skip
    - `redundant.go::checkRedundantOverrides` 改為 per-tenant defaults resolution；nil per-tenant entry 視為 explicit opt-out（`tenantDefaultsLeaves` helper）
    - `cmd/da-guard/main.go::buildCheckInput` 從 `ScopedTenants` 取 `TenantOverridesRaw` + `MergedDefaults` 直接 thread 進 guard input
  - **Customer template note**：客戶可整份 copy workflow 到自己 repo gate `_defaults.yaml`；`Build da-guard` 步驟假設 threshold-exporter Go module 同 repo，純消費 release binary 的客戶可改下載 `tools/v*` release asset（C-11 後配套）
  - **新測試**（`-race -count=2` 穩定）：guard_test 4 cases 涵蓋 NewDefaultsByTenant per-tenant maps / 與 NewDefaults 的 precedence / nil entry skip / tenant 兩 source 都缺 silent skip；scope_test 2 cases 驗 EffectiveConfig 新欄位 populated + snapshot 不 alias EffectiveConfig；da-guard cmd 3 cases 驗 redundant warn surface / `--warn-as-error` exit 1 / cascading 正確性（tenant-db redundant、tenant-web 不 redundant）
  - **C-12 軌道完結**（PR-1 schema → PR-2 routing → PR-3 cardinality → PR-4 CLI → PR-5 GH Actions + warn-tier）。客戶從 pre-commit hook 走 da-guard binary、從 PR 走自動 sticky comment。下一步是 C-11 release packaging 把 binary ship 出去
  - 詳見 `components/threshold-exporter/README.md` § da-guard CLI / planning §12.2 Phase .c row C-12

- **Phase .c — C-12 Dangling Defaults Guard PR-4 — `da-guard` CLI + `da-tools guard` 包裝（v2.8.0）** — 把 PR-1/2/3 累積的 `internal/guard` library 變成 customer-runnable 工具：
  - **新 Go binary** `components/threshold-exporter/app/cmd/da-guard/`（同 module，import `pkg/config` + `internal/guard`），flags：`--config-dir`（必填）`--scope`、`--required-fields`、`--cardinality-limit`、`--cardinality-warn-ratio`、`--format md\|json`、`--output`、`--warn-as-error`、`--version`。Stable exit codes：0 clean / 1 errors found / 2 caller error
  - **新 helper** `pkg/config/scope.go::ScopeEffective()`：給定 conf.d root + 子目錄 scope，遍歷子目錄列出所有 tenant ID，per-tenant 走 `ResolveEffective` 拿到 effective config map。Containment guard（scope 跑出 root 之外 → caller error）+ duplicate-tenant detection（match `ResolveEffective` 既有 loud-fail 立場）+ 排序 deterministic 輸出
  - **Python wrapper** `scripts/tools/ops/guard_dispatch.py` 加 `da-tools guard <subcommand>`，subcommand `defaults-impact` shell-out 到 `da-guard` 二進位。Binary 解析順序：`--da-guard-binary` flag → `$DA_GUARD_BINARY` env → `$PATH` 上的 `da-guard` → friendly install hint。Bilingual help（zh / en）跟既有 da-tools 一致
  - **Honest scope simplification vs planning §C-12**：planning 描述的是「給定 _defaults.yaml change 預測影響」delta-aware 模型，PR-4 ship 的是「驗證當前工作樹」current-state 模型。CI / pre-commit 時兩者等價（變更已寫到磁碟才 run guard）。Speculative simulation 留 C-7b `/simulate` 或後續 PR
  - **Redundant-override warn check 暫不啟用**：guard library `TenantOverrides` + `NewDefaults` 兩個欄位本 PR 不傳，把 redundant-override warning 留待 PR-5 GitHub Actions wrapper 一起 ship（避免 PR comment 在沒有 reviewer-friendly UI 前就 spam）。三個 error-tier 檢查（schema / routing / cardinality）全可用
  - **39 new tests / 全 lint 綠**：Go 11 scope_test cases（whole-tree / sub-scope / containment / dup-tenant / empty / missing-dir / hidden-skip / determinism / symlink）+ 17 da-guard main_test cases（happy / missing-required / unknown receiver / cardinality exceed / cardinality warn / warn-as-error / empty scope / 4 caller-error paths / JSON output / file output / determinism / version / dup tenant / splitNonEmpty / Windows separators）+ Python 11 test_guard_dispatch cases（help / unknown subcommand / 3 binary resolution paths / argv passthrough / exit-code passthrough / FileNotFound / OSError）。`-race -count=2` 穩定
  - **Out of scope, deferred to PR-5**：GitHub Actions workflow file 寫 `.github/workflows/guard-defaults-impact.yml` + post Markdown 報告為 PR comment + redundant-override warn 啟用
  - 詳見 `components/threshold-exporter/README.md` § da-guard CLI / planning §12.2 Phase .c row C-12

- **`docs/internal/bench-gate-rollout.md` — Pre-tag Bench Regression Gate 3-phase rollout plan (v2.8.0, closes [#76](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/76))** — promote the 3-phase plan from issue #76's body into a searchable + cross-refable internal doc. Sections：(1) why staged rollout (variance evidence from PR #59 v1→v2), (2) Phase 1 ✅ (含 #117 release attachment), (3) Phase 2 entry conditions (28 nightly + CV ≤25% + max/min ratio ≤1.30) + acceptance criteria + rollback policy, (4) Phase 3 entry conditions (Larger Runners + 8-week Phase 2 stability), (5) window invalidation triggers, (6) review-firing procedure for issue [#67](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/67) (target ~2026-05-23). `benchmark-playbook.md` §v2.8.0 1000-Tenant Hierarchical Baseline 加 cross-ref。Phase 2/3 implementation 仍為 separate-issue scope per #76 spec — 此 PR 只做 acceptance #1 (doc) + 確認 acceptance #3 (planning §12.5 row) 已存在。

- **Phase .c — C-9 Profile Builder PR-2 — Proposal artifact emission (v2.8.0)** — 給 PR-1 cluster engine 加 emission 層：把 ProposalSet 轉成可寫入 git 的 artifact 目錄樹 (per-proposal `_defaults.yaml` + per-tenant `<tenant>.yaml` + `PROPOSAL.md`)。**Honest scope redirect from spec**：planning row 描述的是「conf.d-ready emission」但 PromRule expr (`avg(rate(...))>0.85`) → threshold-exporter conf.d 結構化欄位 (`cpu_avg_rate_5m: 0.85`) 需要 PromRule→threshold translator，那層不在 PR-2 scope (留給 PR-3 + ADR-019)。PR-2 因此 ship 「intermediate proposal artifact」格式：
  - **`EmitProposals(input)` 純函式**（`profile/emit.go`）— 接 `ProposalSet` + `[]parser.ParsedRule` (corpus index) + `EmissionLayout` (caller 提供 proposal→dir mapping，PR-2 不推斷 directory structure)，回傳 `map[path][]byte`。Caller 寫 disk / stage git / pipe。同 C-7a InMemoryConfigSource / C-10 batchpr.Plan 「IO at the edges」pattern
  - **每 proposal 三 artifact**：`_defaults.yaml` (shared template + dialect + provenance + confidence)、`<tenant>.yaml` per member (varying labels only — shared 不重複)、`PROPOSAL.md` (人類 review summary)
  - **Tenant-key heuristic**：scan VaryingLabelKeys，prefer `tenant`，fallback to first sorted varying key
  - **Safe filename**：`/` and `\` → `-`、leading dots stripped、empty → `_unknown`
  - **Determinism**：yaml.v3 對 `map[string]any` 已 sort keys；output bytes 兩次跑 byte-identical (`TestEmit_DeterministicOutput` lock)
  - **Warning paths (不 fail)**：empty `Layout.ProposalDirs[i]`、`MemberRuleID` 不在 `AllRules` lookup、rule 缺 tenant label → `Warnings` 但其他繼續 emit。**Fatal**：`ProposalSet == nil` / 0 proposals / layout length mismatch
  - **15 new tests / 43 total profile tests**。`-race -count=2` 穩定 1.2s；full suite `-race` 5.4s 無 regression
  - **PR-2 scope discipline** — 不做：translator (PR-3 + ADR-019)、actual conf.d/ 直接 emission、auto-derive layout、interactive UI (PR-4)
  - **PR-3 接口已預留**：emission 已帶 `source_rule_id` provenance、`tenants:` wrapper 已對齊 ADR-018 `extractTenantRaw` 形狀。Translator in-place 把 `_defaults.yaml` 改成 conf.d 形而不動 emit 層
  - 詳見 planning §12.2 Phase .c row C-9

- **`release-attach-bench-baseline.yaml` workflow — closes [#60](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/60) Phase 1 acceptance #6（v2.8.0）** — release-time bench-baseline asset attachment. Triggered on `release: published`：找最近一次成功的 nightly `bench-record.yaml` artifact → download → rename 為 `bench-baseline-<tag>.txt`（tag `/` → `-`）→ `gh release upload --clobber`。Non-blocking — 若無 nightly artifact 或 download 失敗就 log warning 不擋 release。Why nightly artifact vs fresh bench: fresh bench 在 release-time runner ~10-15 min（13 benches × count=6），nightly cron 03:00 UTC runner contention 最低、數字更代表性、attach 只 ~30s。`github-release-playbook.md` 加 §Step 4.5 文檔化。Phase 1 acceptance 6/6 全綠、issue #60 closeable。Phase 2/3 進度仍由 [#67](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/67) (readiness review) + [#76](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/76) (3-phase plan codification) 各別追蹤。

- **`docs/internal/security-audit-runbook.md` — Security Audit Runbook（v2.8.0, dogfood from umbrella [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100) Q2 2026 audit）** — 把這次完成的 6 PR + 1 closed-as-implemented audit campaign 沉澱成 living document。涵蓋 10 章 + 2 附錄：(1) When to run（release 前必跑、CVE 公告 trigger）(2) Pre-audit setup（trivy via docker，DB cache）(3) Inventory phase（grep patterns 8 種）(4) Scan phase（current vs target 雙比對）(5) Reachability triage（5 種 false-positive filter：32-bit-only / BSD-only / CMS path / image processing / specific data source）(6) Built-image vs tag-scan（**最重要 lesson**，#99 的核心發現：scan base tag ≠ scan our actual image）(7) Issue → PR cadence（umbrella + sub-issue + 合併同檔 issue）(8) PR mechanics（commitlint scope=audit、sandbox+win-commit 路徑、CHANGELOG 樣板）(9) Conflict patterns（`### Security` rebase chain、k8s+helm drift、force-push 授權）(10) 收尾 / 後續（umbrella close、upstream 等待類追蹤）+ 附錄 A（trivy false-positive 速查）+ 附錄 B（commitlint scope）。Cross-ref `windows-mcp-playbook.md` §修復層 C.1 / `dev-rules.md` #12，不重寫 sandbox 細節。下次 audit（Q3 2026）直接跑此 runbook，遇新坑 in-place 更新。

- **`make lint-docs-mkdocs` + `scripts/tools/lint/mkdocs_strict_check.sh`（v2.8.0, 自 #113 self-review fallout）** — 補上 mkdocs strict build 的本地 fast-feedback channel。問題根因：`check_doc_links.py` (pre-commit auto) 用 **filesystem** 語意解析 link，而 `mkdocs build --strict` (CI only) 用 **site-root** 語意 (`docs/` 當 root)，兩者對 `../../CHANGELOG.md` from `docs/internal/foo.md` 給出不同結論 — 前者 pass (filesystem 真的跳兩層到 repo root)、後者 fail (跳出 site)。#113 因此 push 後才被 CI 擋。新 script 是 single source of truth：`.github/workflows/docs-ci.yaml` 的 `MkDocs Build Verification` job 與 local `make lint-docs-mkdocs` 都呼叫它，filter 邏輯（5 個 known-acceptable warning patterns）只活在 script 內 (DRY)；workflow 從 ~40 行 inline shell 收斂到一行 `bash scripts/tools/lint/mkdocs_strict_check.sh`。`dev-rules.md` #4 同步加 bullet：動 `docs/**.md` 的 PR push 前必跑此 target。Tier 2 (pre-commit manual stage hook) 暫不做，待 1-2 季 ROI 觀察。

- **Phase .b — B-1 Phase 2 e2e harness 首批 baseline + design archive (v2.8.0, S#37)** — 收尾 PR #80 deferred 的兩條 design §9 acceptance：
  - **§9.3 acceptance** — synthetic-v2 1000-tenant 與 5000-tenant 各跑 30-run baseline land 入 `benchmark-playbook.md` §「首批 baseline 數字」table。1000-tenant：fire P50/P95/P99 = 4748.5 / 4953.95 / 4977.88 ms（[run 24951460457](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/24951460457)）；5000-tenant：4763.5 / 4971.55 / 4984.07 ms（[run 24955478536](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/runs/24955478536)）。Bootstrap 95% CI not too wide on either run. **Near-flat e2e at P95 across 1000 → 5000 tenants** (+0.4% on fire P95) — 證實 design §5.4 預測 (e2e 主導 latency 是 5s scrape quantization, 不是 exporter scan)
  - **§9.4 acceptance** — `docs/internal/design/phase-b-e2e-harness.md` → `docs/internal/archive/design/phase-b-e2e-harness.md`，加 archive front-matter (`status: archived`, `archived-at`, `archived-reason`, `superseded-by`)，本文件停留在 implementation 完成時版本，後續變更走 `benchmark-playbook.md`
  - **B-1.P2-g tracker 升 🟡 → 🟢** — Phase 2 e2e 全段完整 land
  - **closes [#83](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/83)** — operational items meta-tracker 同時關閉
  - 詳見 planning archive §S#37 (待開) + S#37d (cycle-1-6 RCA chain) + S#45 (Tier 1 fail-fast follow-up)

- **Phase .c — C-12 Dangling Defaults Guard PR-3 — Cardinality guard (v2.8.0)** — guard 第三 (最後) 個檢查層落地。預測每 tenant post-merge 的 metric 數，比對 caller-supplied `CardinalityLimit` (建議值對齊 `DefaultMaxMetricsPerTenant=500` from `config_types.go`)。兩 tier — error 在 `>` limit、warn 在 `>` `WarnRatio×limit` (default 0.8 = 80%)。動機：`config_resolve.go::ResolveAt` runtime 對 over-limit tenants **silently truncate excess + WARN log**，operators 經常 deploy 後才發現某些 alerts 不 fire — guard 把這個失敗模式拉到 pre-merge：
  - **`checkCardinality(input)` 純函式**（`cardinality.go`）— 單 pass：每 tenant 算 `countMetricKeys(effective)` (skip-list 對齊 ResolveAt 的 `_state_*` / `_silent_*` / `_routing*` / `_severity_dedup` / `_metadata` 5 類 special keys) → 比對 limit → emit error/warn/nothing
  - **Counting model 是 conservative upper bound** — 不模擬 dimensional regex 展開 (`metric{db=~"db[0-9]+"}` 一 key 會在 runtime 變 N thresholds)，所以 dimensional-heavy tenants 可能被 under-count；其他情況 (tenant override 已 disable 的 metrics、`_critical` suffix overrides) 都對。Trade-off 朝「不誤報」傾斜，限制寫進 `cardinality.go` package header
  - **Skip-list lock-step 維護**：`isSpecialKey` 與 `config_resolve.go::ResolveAt` 的 skip-list 必須手動 keep in lock-step，新加 `_*` semantic prefix 須兩邊都改。`TestIsSpecialKey_FullCoverage` 14 cases 守關 boundary 行為 (含 `_routing_extra` prefix-match vs `_severity_dedup_extra` exact-match)
  - **新 `CheckInput` fields**：`CardinalityLimit int` (≤0 disables 整個 check)、`CardinalityWarnRatio float64` (out of [0,1] fallback to 0.8；=1.0 disable warn tier 只報 errors)
  - **新 2 個 `FindingKind`**：`cardinality_exceeded` (error) / `cardinality_warning` (warn)
  - **16 new top-level tests / 60 total guard tests** — countMetricKeys / isSpecialKey full coverage + checkCardinality 各 tier (no-op when disabled / below floor / at/above warn floor / at exact boundary / above limit / custom warn ratio / out-of-range ratio fallback / ratio=1.0 disables warn tier) + multi-tenant independence + integration with CheckDefaultsImpact (PassedTenantCount drop)。`-race -count=3` 穩定 1.0s；full suite `-race` 5.6s 無 regression
  - **PR-3 scope discipline** — 不做：dimensional regex expansion 估算 (need fixture data to calibrate, defer)、ADR-003 cross-ref (planning row 寫的 ADR-003 實際是 Sentinel Alert，cardinality 真正 source 是 `DefaultMaxMetricsPerTenant`，CHANGELOG 註記 honest correction)
  - **C-12 guard 三層 (schema / routing / cardinality) 全到位** — C-10 PR-2 apply mode 啟動時可呼叫完整 guard
  - 詳見 planning §12.2 Phase .c row C-12

- **Phase .c — C-12 Dangling Defaults Guard PR-2 — Routing schema guardrails (v2.8.0)** — 給 PR-1 落地的 `internal/guard/` 加 routing-schema 檢查。**Scope redirect 從 planning spec**：planning §C-12 layer (ii) 寫的是「routing tree cycle detection + orphaned route」，但 codebase 實際 `_routing` model 是 *flat per-tenant block* (一 receiver + optional `overrides[]`，無 cross-references) → cycles 結構上不可能、orphans 在嚴格意義上不存在。實作 cycle detector 純屬 theatre。本 PR 改 ship 對此 model 真正能抓 bug 的 5 項檢查：
  - **Unknown receiver type (error)** — `receiver.type` 不在 `{webhook, email, slack, teams, rocketchat, pagerduty}`。Source of truth: `scripts/tools/_lib_constants.py::RECEIVER_TYPES` (Python/Go 共用)
  - **Missing required receiver field (error)** — 每 type 有自己的 required fields (`webhook`→`url`、`slack`→`api_url`、`email`→`to`+`smarthost`、`pagerduty`→`service_key`、`teams`→`webhook_url`、`rocketchat`→`url`)。Empty-string 也算 missing (mirror `generate_alertmanager_routes.py` 的 truthy check)
  - **Empty override matcher (error)** — override 沒任何 matcher field (`alertname`/`metric_group`/`severity`/`component`/`db_type`/`environment`) → 會 shadow ALL alerts，幾乎肯定是 bug
  - **Duplicate override matcher (warn)** — 兩 overrides 同 matcher fingerprint → 第二條死碼。Canonical fingerprint 用 `encoding/json` (sorted keys) + SHA-256，order-independent
  - **Redundant override receiver (warn)** — override 的 receiver 與 main receiver 結構完全相同 → override 無路由效果
  - **新 `CheckInput.RoutingByTenant` field** — `map[tenantID]map[string]any`，caller 預先解析 `_routing` payload。維持 PR-1 的 zero-dep on YAML/merge engine 設計 — guard package 仍只吃 `map[string]any`
  - **新 5 個 `FindingKind`**：`unknown_receiver_type` / `missing_receiver_field` / `empty_override_matcher` / `duplicate_override_matcher` / `redundant_override_receiver`
  - **21 new top-level tests** (44 total guard tests, 含 PR-1 的 22) — 五 check 各自 happy/sad path、6 個 receiver type all accepted、empty-string 判 missing、matcher order-independence、nil-tenant skip、failing tenant 算入 PassedTenantCount drop。`-race -count=3` 穩定 1.0s；full suite `-race` 5.6s 無 regression
  - **SSOT 漂移 sentinel** `TestReceiverTypeSpecs_KeysMatchExpected` — 提醒未來若 Python 端加新 receiver type 必須同步本 Go 列表 (PR-3 可能補完整 freshness CI gate)
  - **PR-2 scope discipline** — 不做：完整 URL allowlist 驗證 (重複 `_resolve.go` 已有的層) / cross-tenant alert-rule 名查重 (需 rule discovery 跨 scope) / 完整 ADR-019 routing model 改造 (deferred to v2.9.0+)
  - 詳見 planning §12.2 Phase .c row C-12

- **Phase .c — C-12 Dangling Defaults Guard PR-1 (v2.8.0)** — 新 `components/threshold-exporter/app/internal/guard/` package：在 `_defaults.yaml` 變更被 merge 前驗 (a) 該目錄下所有 tenant 必填欄位仍存在；(b) tenant.yaml 是否有與新 defaults 同值的 redundant override。Phase .c 「保護層」— C-10 PR-2 apply mode 將呼叫 guard 確認 Base Infrastructure PR 安全才放行：
  - **Schema validation (`schema.go`, SeverityError)** — 對每 tenant 的 effective config 走 caller-supplied `RequiredFields` (dotted-path) 列表；missing or explicit-null 都 flag (per ADR-018，YAML null 在 override 等於 delete inherited key — 對 required field 等於 opt-out 該 requirement)
  - **Redundant override check (`redundant.go`, SeverityWarn)** — `flattenLeaves` 把 tenant.yaml + new defaults 拍平成 dotted-path leaves；同 path + scalar 同值 → warning「remove the override and rely on inheritance」。**Skip structured values** (map/slice) per documented PR-1 false-positive guardrail
  - **`CheckDefaultsImpact(input)` 純函式 (`run.go`)** — 跑兩個 check + 全域 stable sort (errors before warnings, then by tenant ID, then field path) + 計 `PassedTenantCount` (warnings 不算 fail)；len(EffectiveConfigs)==0 → fatal error (caller 應 skip 而非 invoke empty)
  - **`Plan.Markdown()` PR-comment 渲染 (`render.go`)** — GFM Summary + 分 Errors / Warnings 兩 table；clean run → ✅ sentinel；pipe / newline 在 table cell escape (避破表)
  - **PR-1 contract 故意 zero-dep on YAML / merge engine** — caller (CLI / GitHub Actions wrapper, deferred to PR-4/5) 負責 YAML parse + deepMerge；guard 只吃 `map[string]any`。讓 C-10 PR-2 apply mode (它本來就要 merge 給 emitter 用) 可直接複用結果
  - **22 tests / 23 incl. subtests** `-race -count=3` 穩定 1.0s；full suite `-race` 5.6s 無 regression。涵蓋 path resolver (happy path / empty / explicit null / non-map walk-through) / flatten leaves / 兩 check 各自 no-op 條件 + 正向 + 邊界 / integration (passed-count 排除 erroring tenants / warnings-only-pass / sort errors-first / determinism JSON byte-identical) / Markdown render (nil-safe / all-clear sentinel / 兩 table 都出 / pipe+newline escape)
  - **PR-1 scope discipline** — 故意延後到後續 PR：(a) **PR-2** Routing Guardrails (ADR-017/018 routing tree cycle + orphaned route)；(b) **PR-3** Cardinality Guard (post-merge label cardinality vs ADR-003)；(c) **PR-4** CLI subcommand `da-tools guard defaults-impact` + YAML parse convenience layer；(d) **PR-5** GitHub Actions wrapper post PR comment
  - 詳見 planning §12.2 Phase .c row C-12

- **Phase .c — C-10 Batch PR Pipeline planner PR-1 (v2.8.0)** — 新 `components/threshold-exporter/app/internal/batchpr/` package：消費 C-9 ProposalSet → 產出 ordered `Plan` (一個 Base Infrastructure PR + N 個 tenant chunk PRs)。**Pure planner** — 零 git ops、零 GitHub API、零 disk write。Phase .c 整鏈 (C-8 → C-9 → C-10) 第四環，定義 input contract 給後續 PR-2 (apply mode) 對齊：
  - **Hierarchy-Aware chunking** (planning §C-10 + risk #13)：所有 `_defaults.yaml` 變更 → **單一 `[Base Infrastructure PR]`**；個別 tenant 變更按 `ChunkBy` 分組 → 每個 tenant PR 帶 `Blocked by: <base>` placeholder marker (PR-2 取代成 `#<actual-pr-num>`)
  - **三種 ChunkBy 策略**：`ChunkByDomain` (default，第一 path segment 分組，安全；review 邊界對齊 domain ownership) / `ChunkByRegion` (前兩 segments，更細) / `ChunkByCount` (固定大小 N，當 domain/region 分組產生 lopsided buckets 時用)。**Soft cap** — `ChunkSize` (default 25) 對 domain/region buckets 切 sub-chunks (`<key>/part-NN`)，oversized domain 不會炸 review
  - **`PlanInput` contract** — `Proposals: []ProposalRef` (`profile.ExtractionProposal` 的 slim subset，避免 batchpr 直接耦合 profile package) + `TenantDirs: map[string]string` (caller 提供 tenant→conf.d/-relative dir，planner 不從 filesystem 推斷) + `ChunkBy` + `ChunkSize`。Tenant 在 proposal 但不在 TenantDirs → `Plan.Warnings` (skip from chunking but surface)，**不 fatal** — 讓 caller iterative refine TenantDirs 不損其餘 plan
  - **PlanItem 兩 Kind**：`PlanItemBase` 首項 / `PlanItemTenant` 後續按 chunk 順序。每 item 含 `Title` (穩定生成 — base: `[Base Infrastructure] Import N profiles (prom+metricsql)` / tenant: `[chunk i/N] Import PromRules to <chunk-key>`) + `Description` (Markdown PR body) + `BlockedBy` + `SourceProposalIndices` (反查 PlanInput.Proposals 用，refresh / rollback flow 必需) + `TenantIDs` + `ChunkKey`
  - **Determinism guarantees** — bucket iteration sorted by key、TenantIDs sorted、tenant items 按 `chunk i/N` 順序、`TestBuildPlan_DeterministicOutput` 用 JSON byte-identical 兩次跑 lock；`TestBuildPlan_SourceProposalIndicesMapBackToInput` 守關 source-proposal back-pointer 正確性
  - **`Plan.Markdown()`** — GitHub-Flavored Markdown 渲染：summary 計數區、warnings 區 (有才出)、items apply-order table、per-item 完整 PR description。Future CLI `da-tools batch-pr plan` stdout 用 / C-3/C-4 UI preview pane embed 用
  - **Tests** 18 個 top-level：error paths (empty proposals / ChunkByCount 缺 ChunkSize) / Base PR 永遠 first / 三種 ChunkBy 各自 grouping / soft-cap 分 oversized domain / missing-dir warning / defaults / title numbering / dialect mix / determinism / source-indices back-pointer / `<unassigned>` bucket fallback / Markdown render snippets / nil-plan safety / warnings 條件出現。`-race -count=5` 穩定 1.0s；full suite `-race` 5.6s 無 regression
  - **PR-1 scope discipline** — 故意延後到後續 PR：(a) **PR-2** `apply` mode (push branches + open PRs via GitHub API)；(b) **PR-3** `refresh --base-merged` (rebase open tenant PRs + semantic-drift report, planning §B4)；(c) **PR-4** `refresh --source-rule-ids` (data-layer hot-fix mode, planning §B5, reuse C-8 `provenance.source_rule_id` index)；(d) **PR-5** CLI subcommands `da-tools batch-pr {plan,apply,refresh}`
  - 詳見 planning §12.2 Phase .c row C-10

- **Phase .c — C-9 Profile Builder scaffolding PR-1 (v2.8.0)** — 新 `components/threshold-exporter/app/internal/profile/` package：消費 C-8 PR-1 的 `parser.ParsedRule[]`，跑 cluster engine 產出 `ExtractionProposal[]`（「這 N 條 rule 結構相同，可萃取出 `_defaults.yaml`」的提案），是 Phase .c 「禁止 50 份 tenant.yaml 拷貝」承諾的計算核心：
  - **`BuildProposals(rules, opts)` 純函式**（`cluster.go`）— 單 pass 演算法：每條 rule 算 `signature = (normalised expr, for, dialect)` → bucket by signature → bucket size ≥ `MinClusterSize` (default 2) emit `ExtractionProposal`；下限不足的 rule 與 ambiguous-with-empty-expr 落 `Unclustered`
  - **Dialect 在 signature 內** — `prom`-dialect 與 `metricsql`-dialect 即使表面相同也**永不同 cluster**，因為 portability 語義不同；C-9 emission step (PR-2) 必須尊重此邊界
  - **Label partitioning** — 每 cluster 取 `union(labels)` → 每 key 在所有 member 同值 → `SharedLabels`（候選 `_defaults.yaml` 內容），其餘 → `VaryingLabelKeys`（必留 per-tenant override）
  - **Determinism guarantees** — bucket iteration sorted by signature；MemberRuleIDs 內 sorted；VaryingLabelKeys sorted；Proposals output 按 MemberRuleIDs[0] sorted；同 input 兩次跑 `encoding/json` byte-identical（單測有 lock）
  - **Expression normalisation**（`normalize.go`）— 三步：`numericLiteral → <NUM>`、`quotedString → "<STR>"`、whitespace 全部移除。讓 `> 1` 與 `>1` 與 `  >  1` 三種寫法 collapse 成同 signature；同時保留 identifier 內的數字（`http_requests_total_5xx` 不會被誤砍）；不同 fn name (`rate` vs `irate`) 永不 collapse
  - **`EstimatedYAMLLineSavings`** — back-of-envelope (N-1) × shared_field_lines；UI surface 顯示「萃取此 Profile 預計省 YY 行」
  - **`ConfidenceHigh` only in PR-1** — PR-1 只走 identical-signature exact match；medium/low confidence (fuzzier matching) 預留 PR-2/PR-3
  - **Tests** 16 個 top-level / 28 incl. subtests：normalisation per-tenant variants collapse / digits-in-identifier preserved / fn name distinguishability / whitespace robustness / dialect-splits-clusters fixture / label partitioning fixture / MinClusterSize 尊重 / SkipAmbiguous 兩 mode / savings estimate / determinism (JSON byte-identical 兩次跑) / signatureFor dialect-included unit gate；`-race -count=5` 穩定 1.0s；full suite `-race` 無 regression
  - **PR-1 scope discipline** — 故意延後到後續 PR：(a) **PR-2** YAML emission（accepted proposal → 實際寫 `_defaults.yaml` + tenant.yaml overrides，含 ADR-019 directory placement decisions）；(b) **PR-3** ADR-019 Profile-as-Directory-Default 寫入 + 鎖定；(c) **PR-4** UI surface (semi-automatic accept loop, "XX tenants 將繼承此 Profile" 顯示)；(d) Fuzzier matching pass (medium/low confidence)
  - 詳見 planning §12.2 Phase .c row C-9

- **Phase .c — C-8 MetricsQL parser scaffolding PR-1 (v2.8.0)** — 新 `components/threshold-exporter/app/internal/parser` package：把 PrometheusRule CRD YAML 轉成 dialect-classified `ParsedRule` records，作為 C-9 Profile Builder / C-10 Batch PR 的輸入。**單一 parser 單一 AST visitor 策略**（per planning §C-8 R7 糾錯一）：
  - **`AnalyzeExpr(expr)`**（`dialect.go`）— 走 `metricsql.Parse` (superset 涵蓋 PromQL + MetricsQL)；parse fail → `DialectAmbiguous` + 完整 parse error；parse success → AST visitor (`visitFuncNames`) 收集所有 function-call name，比對 `vm_only_functions.go` curated allowlist (~80 fns，按 rollup_/range_/over_time_exotic/histogram_/keep_/label_/bitmap_/time-context 八類整理) → 任一 hit → `DialectMetricsQL` + sorted/dedup'd `vm_only_functions: []string`；否則 `DialectProm`。**Coverage gate** `TestVisitFuncNames_AllExprTypesCovered` 用 reflect-style test 跑 corpus 覆蓋 9 種 metricsql Expr concrete types，未來 metricsql 升級新增 Expr type 不接 visitor → 立刻 fail
  - **`ParsePromRules(yaml, sourceFile, generatedBy)`**（`promrule.go`）— 接受兩種 CRD 形狀：標準 `apiVersion/kind/spec.groups` wrapped 與 VM-operator legacy bare `groups:` unwrapped；multi-document YAML (`---` 分隔) 全部串成單一 `ParseResult.Rules`。Per-rule 跑 dialect 分析；malformed expr → ambiguous + `AnalyzeError` 但不 abort batch；missing `alert` 或 `record` name → warning + best-effort rule preserved；malformed top-level YAML → fatal
  - **Output schema**（`types.go`）：每 `ParsedRule` 含 `Alert`/`Record`/`Expr`/`For`/`Labels`/`Annotations`/`SourceRuleID` (`<source>#groups[i].rules[j]`，C-10 `refresh --source-rule-ids` 反查用) + `Dialect` + `VMOnlyFunctions[]` + `PromPortable` (== Dialect==prom 的便利 flag) + `AnalyzeError`；`ParseResult` 含 `Provenance` block (`generated_by` / `source_file` / `parsed_at` RFC3339 UTC / `source_checksum` SHA-256[:64]) + `Warnings[]`
  - **Tests** 16 個 top-level / 37 含 subtests：`AnalyzeExpr` portable PromQL (6 cases) / VM-only dialect (7 cases incl. case-insensitive + composed + binary-op nesting) / ambiguous syntax error / empty expr；`IsVMOnlyFunction` case-insensitive (8 cases)；`visitFuncNames` AllExprTypesCovered；`ParsePromRules` basic wrapped / VM-only / mixed dialects / unwrapped legacy / ambiguous + missing-name warning / empty input fatal / malformed YAML fatal / wrong-CRD warning / legitimate-empty / provenance stamping。`-race -count=5` 穩定 1.3s。Full suite `-race` 6.0s 無 regression
  - **PR-1 scope discipline** — 故意延後到 PR-2：(a) `prom_compatible: bool` 需 `prometheus/prometheus/promql/parser` heavy dep；(b) `vm_only_functions.yaml` external file + freshness CI gate diff against pinned metricsql release；(c) StrictPromQLValidator + `--validate-strict-prom` CLI flag；(d) CLI subcommand `da-tools parser import` (落 `scripts/tools/`)
  - **Dependency**：`github.com/VictoriaMetrics/metricsql v0.87.0` (+ transitive `metrics`/`fastrand`/`histogram`)
  - 詳見 planning §12.2 Phase .c row C-8

- **Phase .c kickoff — Simulate primitive end-to-end (v2.8.0 C-7a + C-7b)** — `POST /api/v1/tenants/simulate` ephemeral preview endpoint + `ConfigSource` interface refactor。客戶/tooling 可拿 raw tenant.yaml + L0..Ln defaults bytes 預覽 effective config、`source_hash`、`merged_hash`，**零磁碟 IO、零對 WatchLoop 污染**，是 C-8 parser / C-9 Profile Builder / C-10 Batch PR 的前置：
  - **`ConfigSource` interface + `InMemoryConfigSource`（C-7a, `app/config_source.go`）** — 抽出「列舉 YAML 檔案」這層；in-memory 版以 `map[absPath][]byte` 為 backing store。`scanFromConfigSource()` 是 `scanDirHierarchical` 的 in-memory 表親，共用 `collectDefaultsChain` / `NewInheritanceGraph` / `sortStrings` helpers，dedup + chain 規則一致；production `scanDirHierarchical` 維持原樣（仍負責 mtime 收集供 WatchLoop change-detection 用）
  - **`SimulateEffective(req)` 純函式（C-7b, `app/config_simulate.go`）** — 輸入 `{tenant_id, tenant_yaml, defaults_chain_yaml[]}`，內部建合成階層 `/sim/lvl1/lvl2/.../tenant.yaml` + `/sim/.../`._defaults.yaml`，走同一條 `computeEffectiveConfig` + `computeMergedHash` code path，回傳 shape 對齊 `EffectiveConfig`（`source_hash` / `merged_hash` / `defaults_chain` / `effective_config`）。`ErrSimulateTenantNotFound` 對應 HTTP 404
  - **HTTP handler `simulateHandler()`（`app/api_simulate.go`）** — POST-only、JSON in/out、`MaxBytesReader` 1 MiB cap、`DisallowUnknownFields`、`tenant_yaml` / `defaults_chain_yaml` 用 base64 transcoding（避 YAML-in-JSON 的 quote/newline 脆弱）。錯誤碼：400 (parse / 訊息錯) / 404 (tenant 不存在) / 405 (非 POST) / 413 (body 過大)。`main.go` 註冊 `/api/v1/tenants/simulate`
  - **Parity gate `TestSimulate_VsResolve_ParityHash`** — 同一份 (tenant.yaml, L0+L1 _defaults.yaml) 寫 tmpdir 後跑 `ConfigManager.Resolve()`，再 in-memory 跑 `SimulateEffective()`，斷言 `SourceHash` / `MergedHash` byte-identical + effective `Config` map `reflect.DeepEqual`。Drift 即破壞 Phase .c「simulate 是 commit 後 effective 的可信 preview」的承諾
  - **18 tests** (3 layers — pure function 7 / HTTP handler 8 / parity + source enumeration 3)，`-race -count=10` 穩定；full suite `-race` 無 regression
  - 詳見 planning §12.2 軌道 Phase .c

- **B-1 Phase 2 implementation — PR-3 of 3（v2.8.0 B-1.P2-e + B-1.P2-f + g 完工）** — e2e alert fire-through harness 第三回合：aggregator + Makefile target + manual-dispatch GH workflow + playbook completion。**Phase 2 Option β rollout 全部 3 PR 落地完成**；首批 synthetic-v2 baseline 數字待第一輪 workflow_dispatch 後由 maintainer 從 aggregate JSON 抽數填入 playbook。零 production code 變動：
  - **B-1.P2-e — `tests/e2e-bench/aggregate.py`** Python stdlib-only（~310 LoC + 24 unit tests）— 讀 `bench-results/per-run-*.json`、過濾 `warm_up=true`、計 fire/resolve P50/P95/P99 of `e2e_ms`、bootstrap 95% CI（per design §8.5：1000 resamples, percentile of percentile）、stage A/B/C/D percentiles、stage C histogram（quantization noise 主導 per §5.4）。Output: `bench-results/e2e-{ISO}-{kind}.json` aggregate JSON + last-line single-line JSON summary per A-15 convention
  - **B-1.P2-e calibration gate decision logic（per §6.5）**：`synthetic-v*` 永遠 `pending`（baseline 非 customer-validated）；`customer-anon` 與 `--baseline-glob` 指定的最近一次 synthetic-v2 aggregate JSON 比對，差距 ≤ ±30%（可調）→ `passed`，外面 → `failed`（baseline voided + 紅框）；缺 baseline → `pending` with warning
  - **B-1.P2-e tests (24)**：percentile / bootstrap_ci / histogram / `_ci_too_wide` / `determine_gate_status` 5 cases / `load_per_run_files` filter+empty+all-warm-up errors / 4 個 end-to-end aggregate (synthetic baseline / customer-passes / partial failures / all-failed-marks-skipped)
  - **B-1.P2-f — `make bench-e2e` Makefile target** + `scripts/ops/bench_e2e_run.sh` orchestrator（fixture stage → bench-run-{0..N} pre-create per §5.1 → `docker compose up --build --abort-on-container-exit driver` → aggregate → teardown）。`make bench-e2e-aggregate` 額外 target 對既有 per-run JSONs 重算（不重跑 stack）
  - **B-1.P2-f — `.github/workflows/bench-e2e-record.yaml`** workflow — main only, manual `workflow_dispatch` per design §8.1（cold start 5-8 min not for per-PR CI）；`fixture_kind` / `count` / `fixture_tenant_count` 為 input；artifact retention 30 days；gate banner surfaces 在 `GITHUB_STEP_SUMMARY`
  - **g (completion) — `benchmark-playbook.md` §v2.8.0 Phase 2 e2e** 完整化：implementation tracker 7 項全 🟢；新增「跑一輪 baseline 速查」/「Aggregator 輸出與 gate banner 矩陣」/「Customer sample calibration gate operational flow」/「Kill switch — v2.9.0 cut 前未抵達 sample 的 go/no-go review」四節
  - **Phase 2 acceptance per design §9 — partial green**：(§9.1 Code) gauges + scaffolding + ring-buffer receiver + driver-in-compose + double-metric alert + send_resolved 全 ✅；(§9.2 量測協定) fixture pre-create + run isolation + warm-up + fire+resolve + n≥30 bootstrap CI 全 ✅；(§9.3 Output 與 CI) per-run JSON + gate banner + Makefile target + 1000-tenant + pinned image versions ✅，但 **5000-tenant 各 30 runs 報告 deferred** to v2.8.x（workflow input 已預留結構，第一輪 1000-tenant baseline 落地後再開）；(§9.4 文件) playbook entry + CHANGELOG entry ✅，**design doc 升格 + archive deferred**（待第一輪實測數字進 playbook §10 後再做 doc-only PR archive `docs/internal/design/phase-b-e2e-harness.md` → `docs/internal/archive/design/`）；(§9.5 品質閘門) pre-commit clean + customer-anon fallback ✅
  - **DEC-B sign-off path now unblocked**：synthetic-v2 baseline 可隨時跑（manual workflow_dispatch）；customer sample 抵達後可立即跑 calibration gate；v2.9.0 kill switch operational 條件清晰

- **B-1 Phase 2 implementation — PR-2 of 3（v2.8.0 B-1.P2-c + B-1.P2-d）** — e2e alert fire-through harness 第二回合：docker-compose stack + host driver。**Local-only**（per design §8.1，cold start 5-8 min wall-clock 不適合每 PR 跑），所有 stack 檔案落在 `tests/e2e-bench/`。零 production code 變動：
  - **B-1.P2-c — `tests/e2e-bench/docker-compose.yml`** + 三個 service config — 6 services：threshold-exporter（reuse PR #78 二 gauge）/ prometheus v2.55.0（5s scrape + alert eval）/ pushgateway v1.10.0（driver 注入 `actual_metric_value`）/ alertmanager v0.27.0（`send_resolved: true`, `group_by: [tenant]`）/ receiver（custom Go ring buffer）/ driver（Python，**inside compose**, 同 kernel clock 避 skew per §2.4）
  - **B-1.P2-c — `alert-rules.yml`** 採 actual-vs-threshold double-metric 模型（per §4.3）：`actual_metric_value > on(tenant) group_left(metric) user_threshold{metric="bench_trigger"}`，`for: 0s` 避 batching latency
  - **B-1.P2-c — `tests/e2e-bench/receiver/`** Go HTTP server（~200 行 + 7 unit tests）— 1) ring buffer (cap 200, FIFO with wrap)；2) `POST /hook` 接 Alertmanager webhook，flatten alerts[] 為 per-tenant Posts，stamp `received_unix_ns`；3) `GET /posts?since=...&tenant_id=...&status=...` 過濾查詢；4) `GET /healthz`；distroless base 同既有 exporter convention；7 tests cover ring wrap / query filter / `tenant` vs `tenant_id` label preference / Alertmanager flatten / non-AM payload tolerance（pre-push fix：原版 silent-store-empty bug, test caught）
  - **B-1.P2-d — `tests/e2e-bench/driver/driver.py`** Python stdlib-only（~370 行 + 13 unit tests）— 5-anchor measurement protocol per §5.2：T0 (fixture write + actual push)、T1/T2（poll exporter timestamp gauges from PR #78）、T3（poll Prometheus `/api/v1/alerts` for `activeAt`）、T4（poll receiver `/posts?since=`）；fire+resolve 對稱（resolve 階段 `stage_ab_skipped: true` 不量 A/B 因 fixture 不動）；run isolation per `tenant=bench-run-{i}`；warm-up run 0；pushgateway DELETE in `finally:` 避 stale state per §8.2；per-run JSON to `/results/per-run-{i:04d}.json` matching design §2.5 schema
  - **`tests/e2e-bench/README.md`** operator guide（~140 行）+ `.gitignore`（active/、bench-results/、generated fixture YAMLs；保留 `_defaults.yaml` 模板 + `customer-anon/README.md` 說明 sample arrival protocol）
  - **預告 PR-3**：aggregation (P50/P95/P99 + bootstrap 95% CI) + `make bench-e2e` Makefile target + `bench-e2e-record.yaml` workflow (main only, manual dispatch) + playbook 完整化 with 第一份 synthetic-v2 baseline
  - **CI surface 故意窄**：本 PR 不觸發 docker-compose 在 CI 跑（per §8.1），CI 只跑 receiver Go test (~7) + driver Python test (~13) + lint。Compose stack 由 maintainer 本機跑

- **B-1 Phase 2 implementation — PR-1 of 3（v2.8.0 B-1.P2-a + B-1.P2-b + g 骨架）** — e2e alert fire-through harness 的 building blocks。零行為變動，全是新增 metric + 新增 fixture mode + doc skeleton：
  - **`da_config_last_scan_complete_unixtime_seconds` Gauge（B-1.P2-a）** — set in `scanDirHierarchical` success path；e2e harness 5-anchor 模型的 T1 anchor；production 用 `time() - <gauge> > N` 做 stuck-scanner 偵測。Error path 不 set（保持 stale gauge 與「忘了 emit」可區分）
  - **`da_config_last_reload_complete_unixtime_seconds` Gauge（B-1.P2-a）** — set strictly post-atomic-swap in `diffAndReload`；e2e harness T2 anchor。同樣 error path 不 set
  - **`generate_tenant_fixture.py --layout synthetic-v2`（B-1.P2-b）** — Phase 2 主基準 fixture mode，在既有 `hierarchical` layout 上加兩個 realistic-ops 分布：(1) Zipf alpha=1.5 / max_size=6 對 tenant size（多數 1-2 個 threshold override，~15% 4+）；(2) power-law alpha=2.0 / max_depth=3 對 `_metadata` overlay depth（>60% flat、~10% 達 depth=3）。`--seed` reproducible。10 unit tests 含分布 statistical asserts（per S#32 lesson 用 invariant-based 而非 absolute-value）
  - **`docs/internal/benchmark-playbook.md` §v2.8.0 Phase 2 e2e 章節 skeleton（g）** — ops-view 摘要：5-anchor 量測模型對照表、fixture_kind 三態（synthetic-v1 / synthetic-v2 / customer-anon）+ calibration gate ±30% 操作流程、implementation 7-子項 progress tracker、fixture 產出速查指令。Design SSOT 仍在 `design/phase-b-e2e-harness.md`；本節是 ops 視角 cookbook
  - **PR-2 / PR-3 預告**：PR-2 = docker-compose stack + host driver；PR-3 = aggregation + Makefile target + workflow + playbook 完整化（以 synthetic-v2 跑出第一份 baseline）

- **Migration playbook — Emergency Rollback Procedures（v2.8.0 B-4，doc-only）** — `docs/scenarios/incremental-migration-playbook.md` + `.en.md` 新增 `Emergency Rollback Procedures` 章節，覆蓋 Phase .c batch-PR pipeline cutover 後的整批退版流程：(1) 退版順序 = merge 順序的嚴格反序（inner tenant PR 先 / outer `_defaults.yaml` 最後 / cascading defaults 按 inner→outer 退）；(2) WatchLoop debounce 驗收 PromQL 對接 v2.8.0 B-3 加入的 `da_config_reload_duration_seconds_count` 與 `da_config_debounce_batch_size`；(3) Staging rehearsal hard gate（cutover 前 2 週）；(4) 退版時間預算表（基於 PR #59 Phase 1 baseline，1000-tenant / 5000-tenant 各四種動作）；(5) 8-項驗證 checklist 含 `merged_hash` 收斂、PromQL counter delta、Alertmanager Silenced 清空。配合 Phase .c C-10 pipeline。工具層 `make rollback-dryrun` / `da-tools batch-pr rollback` 列入 v2.8.x backlog
- **Issue #76 — pre-tag bench gate 3-phase rollout（issue-only，無 code）** — Spawn task C 落地：開 GitHub issue 規範 Phase 1（已 land PR #65）→ Phase 2（main-only hard gate at 3× median-of-5）→ Phase 3（PR-level after Larger Runners）的 entry conditions、acceptance criteria、Gemini「不要 hard gate」反論點 + PR #59 50% within-run variance 反駁。Phase 2 / 3 實作不在此 issue 範圍

- **Phase .b debounce observability + slow-write stress + PR #69 self-review follow-ups（v2.8.0 B-3 + B-7 + Issue #61 polish）**
  - **B-3 reload-duration histogram** `da_config_reload_duration_seconds` (buckets `[0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30]`s) — 觀測 `diffAndReload` 端到端耗時（scan + per-tenant merge + blast-radius emit + fullDirLoad + atomic swap），`fireDebounced` 與 sync-fallback 都 emit 一次 sample。給 Phase 2 hard SLO sign-off 的 p99 訊號用，以及 driver 「300ms debounce 是否過長」的實證依據
  - **B-3 debounce-batch-size histogram** `da_config_debounce_batch_size` (buckets `[1, 2, 5, 10, 25, 50, 100, 250, 500]`) — 觀測單一 fired window 收斂多少個 trigger（debounce 效用訊號）。Sync-fallback (`debounceWindow=0`) 不 observe，避免 1-樣本污染 p50。p50≈1 表示 debounce 沒在 coalesce；p99 飆破 ~50 是 fsnotify storm 訊號
  - **B-7 slow-write torn-state stress test** (`config_slow_write_stress_test.go`) — 50 個 tenant file 用 5-25ms 隨機 jitter 寫入 + 100ms debounce window，斷言：(a) 寫入過程中 fire count 始終 0（sliding window 不被打斷）；(b) settle 後正好 1 fire；(c) 所有 mutated tenant 的 merged_hash 都跨步；(d) batch histogram 收斂為 1 sample / sum=numFiles。`-race -count=10` 穩定。Deterministic seed `0xB7505050`
  - **PR #69 self-review (b)** — `TestBlastRadius_MixedTickEmitsThreeDistinctBuckets` 加 `tenant-third` 第三 tenant，現在實際發 3 個 distinct buckets（source/applied + defaults/shadowed + defaults/applied），名實相符
  - **PR #69 self-review (c)** — `defaultsPathLevel` 防禦性 skip `..`-prefixed 段（K8s ConfigMap mount `..data` / `..2026_*` symlink artifact）；production-named dirs 從不以 `..` 開頭，所以是純守關。+5 unit cases
  - **PR #69 self-review (a) — corrigendum（doc-only follow-up）**：PR #69 description 寫的 `da_config_blast_radius_tenants_affected` cardinality 約 ~793 series 算錯。正確算法：`reason ∈ {source, defaults, new, delete}`（4 值）× scope/effect 有效組合（reason=defaults 時 scope ∈ {global, domain, region, env, unknown} × effect ∈ {applied, shadowed, cosmetic} = 15；reason ∈ {source, new, delete} 時 scope=tenant、effect=applied 各 1 = 3）= **18 distinct (reason, scope, effect) 組合**。每組 9 buckets + `_count` + `_sum` = 11 series → 上限 ~198 series。實際 production 通常只看到 `source/tenant/applied`、`defaults/{global,domain,region,env}/{applied,shadowed,cosmetic}` 等真實出現的組合（~10-12），所以 active series ~120-150。原 PR description 的 ~793 數字是把 reason × scope × effect 笛卡爾積（4×6×3=72）誤套在 11 series/組合，且沒扣掉無效組合
  - 詳見 planning §12.2 軌道 B-3 / B-7 / Issue #61 follow-up
 — pre-Phase-2 工具：用 `gh` CLI 拉最近 N 次 `bench-record` workflow 的 artifact，parse `bench-baseline.txt` 後 **per-bench 先取每個 run 的 median**（吸收 within-run 抖動），再對 28 個 per-run median 算 cross-run CV / max-min ratio + GO/NO-GO 決議。對齊 #60 §Phase 2 「3× of median-of-5」框架（median 吸收 jitter，cross-run variance 才是 regression 訊號）。閾值 hardcode #67 acceptance gate（cross-run CV ≤ 25%、max/min ≤ 1.30、≥ 26/28 runs reliability）。Within-run CV 為**獨立資訊欄**（不影響 verdict）— 高 within-run CV 但 cross-run 穩定的 bench（例如 PR #65 nightly 觀察到的 `IncrementalLoad_1000_OneFileChanged` 27.6% within-run）不會被誤判 NO-GO。stdlib only（無 pandas / numpy），Dev Container / Cowork VM / CI 都能直跑。`--ci` flag exit 1 on NO-GO 供 #67 review 自動化；`--cache-dir` 持久化 artifact 避免重複 download；`gh` 未授權時改 friendly error 不丟 stack trace。詳見 [issue #67](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/67)

- **Issue #61 production blast-radius metric `da_config_blast_radius_tenants_affected`（v2.8.0, RFC）**
  - **新 Histogram** `da_config_blast_radius_tenants_affected{reason, scope, effect}`，buckets `[1, 5, 25, 100, 500, 1000, 2500, 5000, 10000]`。`reason ∈ {source, defaults, new, delete}`；`scope ∈ {global, domain, region, env, tenant, unknown}`（widest changed defaults level for `reason=defaults`，`tenant` for source/new/delete）；`effect ∈ {applied, shadowed, cosmetic}`（applied = merged_hash 移動；shadowed = defaults 變動被 tenant override 擋下；cosmetic = comment/reorder 無語義 key 移動）
  - **新 Counter** `da_config_defaults_shadowed_total` — 把原 `da_config_defaults_change_noop_total` 內混雜的「override 擋下變動」案例獨立計數，便於 ops 量化繼承機制擋下多少潛在風暴
  - **語義收窄**：`da_config_defaults_change_noop_total` 改為僅計 cosmetic（comment-only / reorder / unrelated key），不再涵蓋 shadowed。詳見 §Changed 與 ADR-018 amendment
  - **Per-tick group-by emission**：`diffAndReload` 一個 tick 收集 `(reason, scope, effect)` 計數，loop 結束後一次性 `Observe(N)` 多次（不丟 scope 維度）
  - **新 helpers** `defaultsPathLevel` / `widestChangedScope` / `changedDefaultsKeys` / `tenantOverridesAll` / `parseDefaultsBytes` / `classifyDefaultsNoOpEffect` 在 `config_defaults_diff.go`，27 unit + 7 integration test 覆蓋
  - **告警範例**（`k8s/03-monitoring/configmap-rules-platform.yaml`）：`histogram_quantile(0.99, sum by (le)(rate(...{effect="applied"}_bucket[5m]))) > 500` for 10m → P2 警報；`effect="applied"` 過濾避開 cosmetic / shadowed 假觸發
  - 詳見 [Issue #61](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/61)（含 deep-review 兩輪決議）

- **Pre-tag benchmark report — Phase 1 of issue #60 3-phase rollout（v2.8.0）**
  - **`make benchmark-report`** — 1000-tenant baseline 基準測試 → `.build/bench-baseline.txt`，使用既有 `bench_wrapper.sh`（A-15）。regex `_1000(_|$)` 涵蓋 13 支 1000-scale benchmarks：8 個 legacy flat（`Benchmark{ResolveSilentModes,FullDirLoad,IncrementalLoad_1000_NoChange,IncrementalLoad_1000_NoChange_MtimeGuard,IncrementalLoad_1000_OneFileChanged,ScanDirFileHashes,ScanDirFileHashes_1000_MtimeGuard,MergePartialConfigs}_1000`）+ 5 個 hierarchical（PR #59 新加：`Benchmark{ScanDirHierarchical,FullDirLoad_Hierarchical,DiffAndReload_Hierarchical_1000_NoChange,DiffAndReload_Hierarchical_1000_OneTenantChanged,BlastRadius_DefaultsChange_Hierarchical}_1000`）。Legacy flat 仍在生產（v2.7.0 fallback path），同 scale 一併 record 給 trend 分析。預設 `-count=6 -benchtime=3s`；第一個 sample 視為 warmup，由下游 median-of-5 分析（Phase 2）丟棄，target 本身 record 全 6 個。`COUNT` / `BENCHTIME` 可覆寫
  - **`make pre-tag` 串入 `benchmark-report-warn`** — informational only，bench 失敗不阻擋 tag。Phase 1 設計刻意不加 hard gate（issue #60 §Tension：62% CI variance 證據）；maintainer tag 前人眼看 trend
  - **`.github/workflows/bench-record.yaml`** — nightly 03:00 UTC + `workflow_dispatch`，僅 main，artifact 保留 90 天，`GITHUB_STEP_SUMMARY` 內嵌結果。4 週累積 ~28 點數據作為 Phase 2 entry 條件（hard gate at 3× median-of-5 baseline）的判斷基準
  - **與 issue #60 acceptance 對照**：(1) Makefile target ✅；(2) pre-tag wiring ✅；(3) `.build/bench-baseline.txt` 輸出 ✅；(4) Go 版本固定 1.26 與 ci.yml SSOT 一致 ✅；(5) nightly schedule 解決「4-week stability」資料缺口 ✅；(6) release-please attachment ⏭️ 延後（current target 寫入 `.build/`，未串接 release notes asset upload；low-effort follow-up）
  - **不做的事**（明確分階段）：不寫 `benchmarks/baseline.json`（Phase 2）；不加 hard gate（Phase 2）；不引入 `rhysd/github-action-benchmark`（Phase 1 評估後判定 Phase 2 再考慮）；不碰 PR template / commit lint enforcement（Q3 後續獨立工作）；release-please 自動 attach asset（low-priority follow-up）
  - 詳見 issue #60 / planning §4 Phase .b 離場條件

- **Post-merge housekeeping — PR #59 follow-up drift（v2.8.0）**：PR #59（B-1 Phase 1 + B-8）merge 後例行 drift 收尾，獨立 PR 以保留主 PR diff 純淨：
  - **`docs/internal/test-coverage-matrix.md`** 新增「Tier 2 — Performance Benchmarks」章節 + Phase .b 1000+ tenant baseline 子節，登錄 PR #59 加入 `components/threshold-exporter/app/config_hierarchy_bench_test.go` 的 13 支 hierarchical benchmarks（`Benchmark{ScanDirHierarchical,FullDirLoad_Hierarchical,DiffAndReload_Hierarchical_NoChange,BlastRadius_DefaultsChange_Hierarchical}_{1000,2000,5000}` × 4 patterns + `DiffAndReload_Hierarchical_1000_OneTenantChanged`）：每筆登 Tier / Coverage Target / Last Verified（v2.8.0）；附共用 helpers（`buildDirConfigHierarchical` / `reportResourceMetrics` / `bench*AtSize` 驅動函式）說明 + Phase 1 synthetic baseline disclaimer
  - **`benchmark-playbook.md` `verified-at-version`**：已為 v2.8.0（PR #59 同步更新），本 PR 確認無需 bump
  - **`doc-map` / `tool-map`**：`generate_doc_map.py --check` / `generate_tool_map.py --check` 雙雙 clean（無 drift）

- **`docs/internal/pitch-deck-talking-points.md`（v2.8.0, internal sales/business 對話素材）**
  - 從 PR #59 / `f1f14e7` Phase 1 baseline 萃取 4 個對外 talking points：ADR-018 dual-hash quiet edit noOp / 1000-tenant resource footprint / 1000-2000-5000 empirical scaling / Honest baseline disclaimer
  - 每個 section 附「不要這樣講」清單以防 overclaim（不可講「microservices 不會 restart」、不可混淆 dual-hash 與 noOp algorithm、不可把線性外推當實測等）
  - 引用守則表：合約 SLA / marketing 公開數字 ❌；pitch / proposal ⚠️ 須附 Phase 1 synthetic baseline 前綴
  - 詳見 PR #63

- **Phase .b Phase 2 e2e alert fire-through harness design doc（v2.8.0, B-1 Phase 2 prep）**
  - 新增 `docs/internal/design/phase-b-e2e-harness.md` — 描述從 `conf.d/` 寫入 → webhook 收到 alert 的端到端 latency 量測 harness 設計
  - **Measurement model**：5-anchor（T0 driver write / T1 scan-complete gauge / T2 reload-complete gauge / T3 Prometheus alert activeAt / T4 webhook receive）；scrape+eval 交織塌成單一 stage C 不假裝拆；driver 進 compose 同 kernel clock；fire+resolve 對稱量測。需 exporter 加兩個 timestamp gauges（同 PR ~10 行）
  - **Architecture choice**：docker-compose 而非 k3d，論證 K8s networking jitter 落在 5s scrape quantization 解析度以下；ConfigMap-symlink 行為已被 A-8b unit test (PR #54) 覆蓋
  - **Customer sample 採 calibration gate 模型，非 hard blocker**：output JSON 帶 `fixture_kind` × `gate_status` 欄位；synthetic-v2（zipfian + power-law）為 v2.8.0 baseline，customer-anon 抵達後跑 ±30% 校準 gate；v2.9.0 cut 前未抵達觸發 kill switch go/no-go review
  - **Run isolation**：每 run 用獨立 `tenant_id=bench-run-{i}` 避 Alertmanager dedup；fixture pre-create 避 fsnotify create-vs-modify 路徑差異；第 1 run 標 warm_up 不入聚合；n≥30 + bootstrap 95% CI
  - 詳見 design doc §1–§11；acceptance criteria 在 §9（含 exporter gauge / pushgateway service / send_resolved / actual-vs-threshold rule 等具體要求）

- **Phase .b 1000/2000/5000-tenant hierarchical baseline（v2.8.0, B-1 Phase 1 + B-8）— 此 baseline 非 definitive SLO 承諾**
  - ⛔ **重要 disclaimer**：以下數字為 Phase 1 synthetic fixture 量測，**不能直接寫進客戶合約 SLA**。definitive SLO sign-off 需 Phase 2 customer anonymized sample 校準後重跑（DEC-B in planning §10）。下游文件引用須附「Phase 1 synthetic baseline」前綴
  - **11 new Go benchmarks** in `components/threshold-exporter/app/config_hierarchy_bench_test.go`：`Benchmark{ScanDirHierarchical,FullDirLoad_Hierarchical,DiffAndReload_Hierarchical_NoChange,BlastRadius_DefaultsChange_Hierarchical}_{1000,2000,5000}` + `DiffAndReload_Hierarchical_1000_OneTenantChanged`（B-8 blast-radius with `affected-tenants` metric per size）
  - **Pure Go hierarchical fixture helper** `buildDirConfigHierarchical(b, N)` — 8 domains × 6 regions × 3 envs = 144 leaf dirs + `_defaults.yaml` at L0/L1/L2/L3；sync.Once cached across read-only benchmarks + fresh-dir variant for mutating benchmarks
  - **Resource metrics helper** `reportResourceMetrics(b)` — 強制 `runtime.GC()` ×2（reap finalizers）後 emit `MB-heap-after-gc` / `MB-sys` / `goroutines` via `b.ReportMetric`
  - **1000-tenant baseline**（3-run median）：scan 32 ms / fullDirLoad 146 ms / diffAndReload-noChange 189 ms / BlastRadius (21 affected) 212 ms
  - **Scaling characterization（1000 / 2000 / 5000 tenants, 3-run median）**：scan 51/105/273 ms（5×=5.35× → 略 super-linear, +7% over linear）；fullDirLoad 237/570/1097 ms（5×=4.63× → 混合）；BlastRadius 266/535/1308 ms（5×=4.92× → near linear）；affected-tenants 21/42/105（嚴格 linear, geometric expectation）
  - **Memory/goroutines** linear at scale：1000=19 MB sys / 2000=29 MB / 5000=42 MB；goroutines 一律 2（**無 leak signal at 5000-scale**）
  - **Sharding 決策 empirical（not extrapolated）**：≤2000 完全無瓶頸；2000-5000 可運行需 staggering；5000-10000 需配 `diffAndReload` 尾段優化 + GOGC tuning；>10000 強烈建議 sharding。**不從單一 1000 點外推**
  - **benchmark-playbook.md** 新章節「v2.8.0 1000-Tenant Hierarchical Baseline (Phase 1, B-1)」— 完整方法論 + latency/resource 表格 + 3 個量測踩坑（`diffAndReload` 尾段 fullDirLoad 佔時、quiet defaults edit noOp 的 bench design 教訓、`IncrementalLoad` ≠ hierarchical hot path 澄清）
  - **Honest baseline caveats**：Phase 1 synthetic fixture；不含 alert fire-through e2e；non-definitive SLO（待 Phase 2 customer sample 校準）
  - 詳見 planning §12.1 Session #27 / archive §S#27

- **Phase .a wizard.jsx 剩餘 Tailwind 色票 → CSS var 遷移（v2.8.0, A-3）**
  - **Tier A migration（15 個 edit points 遷 ~28 個 Tailwind color class instances → `var(--da-color-*)`）**：`bg-white` ×5 → `bg-[color:var(--da-color-surface)]`；`text-white` on accent bg ×3 → `text-[color:var(--da-color-accent-fg)]`；`text-white` on toast bg ×1 → `text-[color:var(--da-color-hero-fg)]`（toast-bg 與 hero-bg 一致不 theme-flip）；`text-green-*` / `bg-green-*` / `border-green-*` → `--da-color-success(-soft)`；`bg-amber-* text-amber-*` / `border-amber-*` → `--da-color-warning(-soft)`；`bg-indigo-600 text-white` / `bg-indigo-100 text-indigo-700` 按鈕對 → `--da-color-accent(-soft) / -fg`；`ring-indigo-400` focus → `ring-[color:var(--da-color-focus-ring)]`
  - **Tier B 延後項（4 處，加 `// A-3 deferred:` TODO 註解）**：`text-blue-300` toast-link-on-dark（缺 `--da-color-link-on-dark` token）/ `border-indigo-200` 裝飾性藍邊（缺 `--da-color-accent-border-soft`）/ `text-purple-700` 語意 other-path（缺 `--da-color-semantic-other`）/ `bg-gradient-to-br from-blue-50 via-white to-indigo-50` 頁面 hero 漸層（需複合 token 或 inline style 決策）。每處 TODO 寫明所需新 token 名 + 決策條件
  - **已知 UX 微退化**：優先順序卡片的 `hover:bg-amber-100` 被遷成 `hover:bg-[color:var(--da-color-warning-soft)]`，與 normal state 同色 → 懸浮時背景無視覺變化。原因：`--da-color-warning-soft-hover` token 未存在。影響範圍：單一 `.priority` card 懸浮狀態；`border` 與 `shadow` hover 效果仍保留。**Tier B follow-up 候選**：新增 hover 變體 token 或改用 `filter: brightness(0.95)` 替代
  - **dark-mode readiness**：Tier A 遷移後 wizard 色彩隨 `data-theme="dark"` 自動翻轉（原 Tailwind palette 寫死亮模式）；Tier B 延後項仍 light-mode-only，待新 token land 後再遷。**已知驗證缺口**：本 PR 僅以 Dev Container 亮模式 DOM probe 驗證，未視覺驗證 `data-theme="dark"` 下實際渲染；wizard.jsx 不在 `tests/e2e/` 覆蓋，無 CI regression 保護。遷移後 token 翻轉由 `design-tokens.css` 規範承諾保證
  - **驗證**：Dev Container 真 DOM 渲染 + probe（h1 / role 4 matches / 4× `bg-surface` / 1× `bg-accent`）；`check_design_token_usage.py --ci` wizard.jsx 0 violations（仍 45 個 pre-existing 在其他檔案）；JSX Babel standalone parse 通過
  - **Phase .a 軌道一完結**：A-3 merge 後全綠（A-4 / A-14 / A-16 三項延 v2.8.1 除外）
  - 詳見 planning §12.1 Session #26 / archive §S#26

- **Phase .a Playwright E2E fixme 清零 + ESLint 守關（v2.8.0, A-7 + A-13）**
  - **A-7 locator calibration**：`notification-previewer` / `threshold-heatmap` / `rbac-setup-wizard` / `cicd-setup-wizard` 4 spec × 2 `test.fixme()` = **8 條**全數清除。各 spec 改用 `getByRole` / `getByLabel` / `getByText(exact)` 穩定 semantic anchor。針對 wizard 類 fixme #2 的錯假設重新 frame 測試意圖（rbac / cicd 原假設「load 即見 output」，實際要到 step 3 / 5 才渲染，改為驗 step nav 結構）
  - **A-13 ESLint 守關**：新增 `tests/e2e/eslint.config.mjs` flat config，rule `playwright/no-skipped-test` 設 `{ allowConditional: false, disallowFixme: true }`。bare `test.fixme()` / `test.skip()` commit-time 即擋；條件式 `test.skip(isChrome, 'reason')` 仍允許
  - **pre-commit + Makefile 整合**：`.pre-commit-config.yaml` 加 `playwright-lint` hook（scoped `^tests/e2e/.*\.spec\.(ts|js)$`）+ `make lint-e2e` target
  - **`docs/internal/frontend-quality-backlog.md` 新建**：補齊 testing-playbook §v2.7.0 LL §5 引用但不存在的登記檔案；template + A-7 清零歷史 + A-13 cross-ref
  - **testing-playbook §v2.7.0 LL §2 注 "A-13 Enforcement"**：政策從規範 → 自動攔截
  - **Dev Container 主路徑驗證**：48/48 test pass × 3 repeat stability gate（headless `npx playwright test --repeat-each=3`）。方法論：寫 `_calibrate.mjs` probe（`_*` 前綴 gitignored）→ Playwright Node.js API `count()` + `textContent()` = `--ui` locator panel 同等信號，不需 X11 GUI / 不需 user 介入
  - 詳見 planning §12.1 Session #25 / archive §S#25

- **Phase .a commit-msg enforcement bundle（v2.8.0, Issue #53）**
  - **`pr_preflight.py --check-commit-msg` body/footer validation**：加 `validate_commit_msg_body()` helper，每個 post-header 非註解行 > 100 chars → ERROR；缺 blank-line-after-header → WARN。本地 commit-msg hook（PR #44 C2）本來只驗 header，CI commitlint 多驗 `footer-max-line-length ≤ 100`，PR #51 / PR #54 踩到「local 過 CI 擋」走 force-push-with-lease 修的情境至此消除
  - **`make commit-bypass-hh ARGS="-F _msg.txt" [EXTRA_SKIP=...]`**：codified narrow bypass — 只跳 `head-blob-hygiene`（FUSE Trap #57 的唯一合法 bypass case），commit-msg hook + 其他 pre-commit hook 仍跑。替代 sledgehammer `git commit --no-verify`
  - **testing-playbook §v2.8.0 LL #3 更新**：regulation layer → enforcement layer；新規則「FUSE Trap #57 繞道一律 `make commit-bypass-hh`」
  - **`tests/dx/test_preflight_msg_validator.py` 20 → 29 tests**：9 條新 body/footer 驗證（long line ERROR / 恰 100 chars 邊界 / 缺 blank-line-after-header WARN / comment 行不計 / 自訂 max_length / empty msg / CLI 端 rejection / CLI 端 warnings-only 仍 pass）
  - Dogfood：本 PR 的所有 commit 走 `make commit-bypass-hh`，commit-msg validator 自己驗自己。closes Issue #53

- **Phase .a Scanner correctness + test harness bundle（v2.8.0, A-10 fix + A-8b + A-8d + LL ext）**
  - **A-10 product fix — WatchLoop hierarchical scan awareness**（`components/threshold-exporter/app/config.go` WatchLoop block, [Issue #52](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/52)）：root cause — WatchLoop 在 hierarchical mode 下仍用 flat-only `scanDirFileHashes`（`os.ReadDir(dir)` + `IsDir()` skip），看不到 `conf.d/<domain>/<region>/tenant.yaml` 等 nested tenant file 的變動，所以 file 改動**永遠不觸發 reload**；測試靠 `os.Chtimes(now-5s)` 詭技偶然湊效率才 pass。改為在 hierarchical mode 走 `scanDirHierarchical`（recursive）+ per-file hash diff。`TestWatchLoop_DebouncedReload_DetectsFileChange` 從 Chtimes 版本改為直接寫檔觸發，dev container `-race -count=30` → **30/30 PASS**（修前 1/30 flake）
  - **A-8b `TestScanDirHierarchical_K8sSymlinkLayout`**（planning §12.2）：K8s ConfigMap mount pattern invariant lock — file-symlinks ARE followed (`os.ReadFile` resolves)、dir-symlinks NOT recursed（`filepath.WalkDir` lstat semantics），防止未來 Go stdlib 升級悄悄 regress
  - **A-8d `TestScanDirHierarchical_MixedValidInvalid` + 新 metric `da_config_parse_failure_total{file_basename}`**：poison-pill chaos — malformed YAML 不污染 sibling 正常 file 的發現 / 掃描；broken file 仍進 hash 表（change detection 可感知 recovery）；**新 Counter** 提供「tenant 檔持續損壞」的 ops observability signal（Gemini R3 #3 原提案，per-file error-skip 邏輯本就存在，這批純加 metric exposure + behavior lock）
  - **Testing-playbook §v2.8.0 LL #3 extension** — 本地 commit-msg hook（PR #44 C2）**只驗 header**，不驗 body/footer；CI commitlint 多驗 `footer-max-line-length` 等 body 規則。PR #51 self-review commit 本地過、CI 擋（long pytest path 被當 footer），force-push 修。暫時 mitigation + 長期 enforcement 併入 Issue #53
  - A-8c 已於 PR #51 merge；A-8 family 至此 b/c/d 三件齊。僅 A-8 Golden Parity `hypothesis` 擴充（基礎已在 codebase）還可後續做

- **Phase .a Dev Container enablement bundle（v2.8.0, PR #51 接手: Trap #62 + A-12(v) + A-8c + testing LL）**
  - **Trap #62** `windows-mcp-playbook.md` — dev container mount scope drift workaround（cp-test-revert workflow for editing claude worktree files that need to run in container）
  - **A-12(v) `scripts/session-guards/git_check_lock.sh` hardening** — self-PID + name filter（Trap #58 long-term fix）+ `.git/HEAD` NUL-byte corruption auto-repair（Trap #59 long-term fix）+ dedicated `--check-head` subcommand + 14 pytest cases
  - **A-8c `TestConfigManager_DeletedTenantCleanup`**（`config_hierarchy_integration_test.go`）— behavior-lock test for the delete path's atomic-swap; verifies all 4 per-tenant maps + `inheritanceGraph.TenantDefaults` clear the deleted tenant in one swap, no collateral damage to siblings, no goroutine leak. `-race -count=30` clean in Dev Container
  - **testing-playbook.md §v2.8.0 LL** — codify 3 patterns 從 PR #49/#50/#52 踩到的: subprocess CLI test 不計 coverage / 本地輸出截斷要二次驗證 / `--no-verify` 僅跳 FUSE Trap #57 不跳 commit-msg
  - **Version consistency drift fix**（承接 PR #51 中繼 commit）：`design-system-guide.md` front-matter 回 v2.7.0、`windows-mcp-playbook.md` Trap #58 body `v2.8.0-planning` 包 backtick 避開 `bump_docs.py` regex
  - A-10 product race 留 [Issue #52](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/52)（另 PR 處理 `config_debounce.go` 的 atomic-swap empty window）；`--no-verify` 長期 enforcement 留 [Issue #53](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/53)
  - 詳見 `v2.8.0-planning-archive.md §S#21`

- **Phase .a SSOT drift cleanup bundle（v2.8.0, A-2 + docs/design-system-guide 殘留 refs + §12.4 traps codify）**：延續 PR #49 的 bundle 做法，把 Phase .a 軌道一剩下的純文件收尾一次處理。三項同屬「code/canonical state 已往前走，但 authoritative docs 沒跟上」的 SSOT drift 類型，彼此零耦合但 theme 一致：
  - **A-2 — `docs/interactive/changelog.html` 補 v2.1-v2.6 時間線（REG-003 resolved）**：原檔只有 v2.0.0 + v1.10-v1.13 五張卡片，缺 v2.1 ~ v2.6 共 6 個 minor release。手工新增 6 張 `version-entry` card（對應 `CHANGELOG.md` L377-895 各版 release notes 的 highlight 摘要 + date + hl-tag + stats-bar），v2.6.0 標記 `badge-current CURRENT`，v2.0.0 移除過時的 CURRENT badge。**REG-003** 登記於 v2.7.0 CHANGELOG L172（「REG-003 changelog.html 修復延至 v2.8.0」）至此 resolved；`known-regressions.md` 本身已於 Session #16 per radical-delete policy phantom-deleted（registry 與 main 代碼一致即可不寫 retro-narrative），故 REG-003 resolution 不再回寫 registry row，僅記於本條。
  - **`docs/internal/design-system-guide.md` 殘留 stale token refs 更新**：PR #34 Token Audit 完成後 guide 未同步——(i) §3.1 整段 `primary/secondary` 家族描述改寫為 `accent/hero-*/tile-*` canonical namespace（對應 PR #34 → PR#1c 的 token-split），附 token 歷史導覽段；(ii) §3.3 Icon 色表 6 row 全部更新（validation/cli/rules/wizard/dashboard/chart 的 light+dark 值與 `design-tokens.css` SSOT 對齊，如 icon-cli `#2563eb` → `#f59e0b` 是語意改色不是值 drift）+ 附註 `*-bg` variant 配套使用規則；(iii) §3.4 `phase-*` → `journey-*` token 整段 rename（token 名 + 值 + dark mode 欄同步）；(iv) §3.5 mode-silent/mode-maintenance 兩 row 色值對齊；(v) 3 處內文 `--da-color-primary` 引用全改 `--da-color-accent`（§Category table / focus-visible CSS / legacy alias example）；(vi) L26 次要文字 `#64748b` → `#475569`（TECH-DEBT-007 post-fix canonical 值）；(vii) front-matter `version: v2.7.0 → v2.8.0`（內容已反映 v2.8.0 canonical token 狀態）。
  - **`docs/internal/windows-mcp-playbook.md` 新增 trap #58-#61**（codify `v2.8.0-planning.md §12.4` 中仍 open 的環境 trap）：#58 `make git-preflight` 把自身 bash 當活 git 程序誤判（self-PID filter missing；手動繞道 + 長期解 A-12 子項 v）；#59 `.git/HEAD` 被 NUL byte 填到 57 bytes 使 `git rev-parse HEAD` fatal（FUSE write-cache loss 的變體，附 `printf 'ref: refs/heads/<br>\n' > .git/HEAD` 直接 rewrite）；#60 `generate_doc_map.py` 長 I/O regen 在 FUSE 被 fsync 中斷 → HEAD corruption + 全檔假 "new file"（`make recover-index` 救急 + 長期 `--safe` atomic rename 提案）；#61 PowerShell `Out-File -Encoding utf8` / `Set-Content -Encoding utf8` **強制加 BOM** 使 commit message 首字被污染、commitlint 連環 fail（與 pitfall #32 JSON BOM 區別，這條針對 commit-msg；正解 `[IO.File]::WriteAllText($p, $m, [Text.UTF8Encoding]::new($false))` + filter-branch batch de-BOM SOP）。
  - **`docs/internal/{testing,benchmark,windows-mcp,github-release}-playbook.md` `verified-at-version: v2.7.0 → v2.8.0`**：dogfood PR #49 A-6 新工具 `bump_playbook_versions.py --to v2.8.0` 4 檔一次 bump 成功（驗證工具正確性 + 對齊本批 trap 新增後的複查狀態）。

- **Phase .a 軌道一 DX polish bundle（v2.8.0, A-6 + A-9 + A-11）**：Phase .a 軌道一三個散落的小額收尾合併成單一 PR。三項彼此獨立、都純 Python + markdown，無 Go / Playwright runtime 依賴；合併成 bundle 是因為 scope 接近（都是「把既有手動流程或設計契約 codify 為工具 / 章節」），拆成三個 PR 只會稀釋 review 訊號。
  - **A-6 `scripts/tools/dx/bump_playbook_versions.py`**：tag 切版時 bump 4 份 operational playbook（`testing-playbook.md` / `benchmark-playbook.md` / `windows-mcp-playbook.md` / `github-release-playbook.md`）的 `verified-at-version:` front-matter 欄位。刻意**不**合併進 `bump_docs.py` — 後者是**元件版號**（platform/exporter/tools/tenant-api）的 SSOT，playbook 的 `verified-at-version` 是**人工複查戳記**，語意不同。CLI 支援 `--to vX.Y.Z` / `--check`（CI 偵測落後）/ `--dry-run`（印 diff 不寫）。Byte-faithful：用 `read_bytes` + `write_bytes` 避免 `read_text` 預設的 universal-newline translation 吃掉 CRLF（test harness 實測出這個 bug）。ASCII-only 輸出（pitfall #45 精神延伸，`->` 而非 `→`）。`dev-rules.md` 的 `verified-at-version` 刻意**不**列入 scope，維持與 `check_playbook_freshness.py::PLAYBOOK_PATHS` 同步。**19 tests** (`tests/dx/test_bump_playbook_versions.py`) 覆蓋 UPDATED / OK / MISSING 三狀態 + `--check` / `--dry-run` 模式 + LF/CRLF 行尾保留 + idempotent + 其他 front-matter 欄位不動 + 版號格式驗證。
  - **A-9 `scripts/tools/lint/check_path_metadata_consistency.py`**：實作 `docs/schemas/tenant-config.schema.json::definitions.metadata.$comment` 的設計契約（ADR-017）— 當 conf.d/ 目錄階層（`domain/region/env/`）與 tenant `_metadata.{domain,region,environment}` 不一致時發出**警告但不阻擋**（always exit 0），schema 已允許 override，此工具只負責 surface drift。保守啟發式：只對第一層目錄映射 `domain`；只對允許清單 `{prod, production, staging, stage, dev, development, test, qa}` 內的 segment 映射 `environment`，不夠把握的 case 完全不警告（避免 fixture / 非標準命名的 false positive）。`_*.yaml` 檔案跳過（defaults/policies/profiles 不是 tenant 檔）。註冊進 `scripts/tools/validate_all.py::TOOLS` 並新增 `.pre-commit-config.yaml` manual-stage hook `path-metadata-consistency-check`（scope `^components/threshold-exporter/config/conf\.d/.*\.yaml$`）。CI 模式輸出單行 `<file>:0: warning: ...` 格式便利 GH Actions annotations / `grep`。**16 tests** (`tests/lint/test_check_path_metadata_consistency.py`) 覆蓋 path inference / environment mismatch / domain mismatch / 大小寫不敏感 / `_*.yaml` 跳過 / malformed YAML 不 crash / CI 模式格式 / missing config dir soft fail。現有 repo 乾淨（0 mismatch）。
  - **A-11 `docs/internal/github-release-playbook.md §PR CI 診斷流程`**：新章節，專治 Cowork VM proxy 封 `api.github.com` + Desktop Commander shell 找不到 `gh` 時，從 Windows 側走 `curl.exe` + REST 排 PR check 失敗的標準路徑。核心內容為四段 URL ladder（`/pulls/{n}` → `/commits/{sha}/check-runs` → `/actions/runs/{id}/jobs` → `/check-runs/{id}/annotations`），特別點出 `/actions/jobs/{id}/logs` 常回 403，改打 annotations endpoint 拿 human-readable 錯誤訊息。環境層陷阱（PowerShell BOM / `Invoke-RestMethod` timeout / `gh pr checks` JSON 沒 `conclusion` 欄位 / stacked PR DIRTY state 靜默跳過 CI）**不複製** 回本章節，改以 table 交叉引用 `windows-mcp-playbook.md` pitfall #28 / #31 / #32 / #50 / #56；doc-as-code 原則避免雙處 SSOT drift。附 PowerShell 最小可用診斷 snippet（`curl.exe -H Authorization: Bearer` + `ConvertFrom-Json` + `Where-Object`）。Quick Action Index 新增一列指向本章。

- **Pitfall #45 byte-level enforcement（v2.8.0 Phase .d, PR #45, branch `feat/v280-bat-ascii-purity`）**：把 pitfall #45（Desktop Commander `start_process` 執行 `.bat` 時編碼損壞）從「人工紀律 + playbook 備註」升級為 CI-gated 雙層防線。Dogfood PR #44 的 session resilience 工具鏈時，重新解 root-cause 發現真正的機制是 cmd.exe batch parser 對繼承的 OEM codepage（cp950 / cp437，**不是** cp65001）做 byte-level 讀取，任何 ≥0x80 byte 都可能落在 parser 的 shell metachar 範圍（0x80–0xBF 含 cp1252 標點 continuation byte），**後續幾行**才出現 `@echo off` / `setlocal` / `goto` 「不存在」的假象。`cmd /c` 不救（子 cmd 仍繼承父 codepage），`chcp 65001` 不救（preamble 已用錯 codepage 讀完）。PowerShell 呼 `.bat` 不撞這條路徑是因為 PS runtime 先把 command line decode 成 UTF-16 再交給 cmd。
  - **`tests/dx/test_bat_label_integrity.py` 從 7 → 16 條**：新增 `ALL_OPS_BAT_FILES` 覆蓋所有 `scripts/ops/*.bat`（原本 label integrity 只測 `win_git_escape.bat` / `win_gh.bat`，`dx-run.bat` 漏網）；3 條新 parametrized assertion — `test_bat_files_are_ascii_pure`（每個 byte < 0x80，違規時 print L:col + byte + UTF-8 decoded preview）/ `test_bat_files_are_crlf`（沒有 bare LF）/ `test_bat_files_have_no_utf8_bom`（檔頭不得 `EF BB BF`）。helper `_find_non_ascii(bytes) -> list[tuple[int, int, int, str]]` 回報前 10 筆違規供 pytest 輸出。
  - **`scripts/tools/lint/check_bat_ascii_purity.py` pre-commit L1 hook**（`bat-ascii-purity-check`）：scope 限定 `scripts/ops/*.bat`（其他 `.bat` 如 dev-container bind-mount script 不走 Desktop Commander start_process 路徑，不受 pitfall #45 管），pre-commit 透過 `files: ^scripts/ops/.*\.bat$` 把關。fail 輸出 L:col:byte + UTF-8 decoded preview（前 5 筆）+ 三條修法（替換 CJK / em-dash → `--` / 重存 CRLF / 去 BOM）+ byte-level 根因說明 + playbook pitfall #45 連結。argparse 接受 `paths` 位置參數（pre-commit 傳 staged files），empty 時 fallback 掃全目錄。
  - **ASCII-ify 3 個 `scripts/ops/*.bat` 倖存者**：`dx-run.bat`（`#核心原則` → `"Core Principle" section`）、`win_gh.bat`（em-dash + `§MCP Shell Pitfalls` / `§修復層 C` 4 處）、`win_git_escape.bat`（`§MCP Shell Pitfalls` / `§FUSE Phantom Lock 防治` + em-dash 2 處）。全部保留原 CRLF（驗證：`0d 0a` 出現 23 / 187 / 406 次，無 bare LF）。
  - **`docs/internal/windows-mcp-playbook.md` 兩處更新**：
    - Pitfall #45 row 擴充為 byte-level 根因（OEM codepage 繼承、byte-oriented parser、0x80–0xBF metachar 碰撞、parser 狀態機破壞、為何 `cmd /c` / `chcp 65001` 不救、為何 PowerShell 能過）+ 三條鐵律 + CI gate 引用。
    - §MCP Shell Pitfalls 表第 3 列標注 **CI-gated ✅** + 列出具體 hook / pytest 名稱；章節末段說明「encoding / CRLF / BOM 現已由 pre-commit + pytest 雙層攔截，8.3 short path 與 em-dash 引號仍人工紀律」。
  - **Dogfood chain 完整**：本 PR 在 FUSE phantom lock + stuck stale index 狀態下執行，走 Windows 側 `_phantom_unlock.ps1` → `_branch_create.ps1` → `_cleanup.ps1` plumbing escape hatch 配合 Desktop Commander `start_process`，RED（3 新 test 紅）→ surgical edit → GREEN（16 條全綠 + 合成的 bad .bat 被 hook 拒），完整驗證 PR #44 的逃生門工具鏈。

- **Session resilience + token-economy bundle（v2.8.0 Phase .c, PR #44, branch `feat/v280-session-resilience-bundle`, 8 commits: C1–C8）**：解決 Cowork FUSE mount 下反覆踩到的兩類 showstopper——(a) `.git/index.lock` / `.git/HEAD.lock` 幻影鎖讓所有 `git add` / `commit` / `update-ref` 直接 fail、(b) `.git/index` 被寫壞後 `git status` 以下全部不可用。同步把 commit-msg 驗證從 CI-only 搬到本地、把 pre-push marker gate 做成 PR-state 感知，整組落成「code-first 逃生門」：
  - **C1 `.commitlintrc.yaml` 擴展 enum**：`type-enum` 加 `chore` / `revert`，`scope-enum` 加 `config` / `resilience` 對應 PR #44 本身的類別。既有 Conventional Commits 家族不變。
  - **C2 `scripts/hooks/commit-msg` + `scripts/tools/dx/pr_preflight.py --check-commit-msg` / `--check-pr-title`**：把 commitlint 檢查本地化。`commit-msg` hook 由 session-init 自動安裝進 `.git/hooks/`（見 C6）；`pr_preflight` 新增兩個離線子命令：
    - `--check-commit-msg <file>`：讀 commit msg file → 解析 header → 對 `.commitlintrc.yaml` 的 `type-enum` / `scope-enum` / 長度上限驗證。fail 時列明違規項 + 修正建議。
    - `--check-pr-title <string>`：同樣的驗證邏輯，但輸入是 PR title。CI 端用來擋 PR title drift（跟 commit header 不同步的經典坑）。
    - `_read_commitlint_enum()` 不依賴 PyYAML，block-style flow 手解，對應 repo 現行 YAML 格式。
    - **新測試 `tests/dx/test_preflight_msg_validator.py`** 覆蓋合法/違法 type/scope/長度/空白字元/CRLF 結尾 etc.
  - **C3 `scripts/ops/fuse_plumbing_commit.py` + `make fuse-commit` / `make fuse-locks`**：幻影鎖場景下的 commit 逃生門。當 `.git/index.lock` 以 EPERM 狀態存在（`ls` 看得到、`rm` 失敗、`git` 拒絕 create own lock）時，走 git plumbing：`hash-object -w <file>` → 建 `GIT_INDEX_FILE=/tmp/plumb_idx_...` 的 temp index → `update-index --add --cacheinfo` → `write-tree` → `commit-tree` → 直接 write `.git/refs/heads/<branch>`。完全跳過 `.git/index` + `.git/index.lock` 的 handshake。三種 mode：
    - `--auto --msg msg.txt file1 file2` — 偵測幻影鎖 → 有則 plumbing、無則 normal path（hooks 有跑）
    - `--force-plumbing` — 永遠走 plumbing（skip hooks；quality gate 另外由 `make pr-preflight` 把關）
    - `--show-locks` — 列出偵測到的 phantom lock paths（診斷用）
    - `--amend`、exit codes 0/1/2 語意、保留 exec bit、best-effort `.git/index` 同步
    - **新測試 `tests/dx/test_fuse_plumbing_commit.py`** 覆蓋 detect / plumbing path / normal path / amend / ref 寫失敗回報 / exec bit 保留
  - **C4 `scripts/ops/recover_index.sh` + `make recover-index`**：`.git/index` 被寫壞（`index file corrupt` / `index uses ???? extension, which we do not understand` / `index file smaller than expected` / `bad index file signature` / `bad index file sha1 signature`）時的重建路徑。從 HEAD 走 `GIT_INDEX_FILE=$TMP_IDX git read-tree HEAD` 建 temp index，cp 到 `$INDEX.recover.$$` 同路徑 staging → `mv` 覆蓋 `.git/index`（rename(2) 同 FS atomic）。`--check` 模式只診斷（exit 0=clean / 2=corrupt）、預設模式診斷+修復。
    - **新測試 `tests/dx/test_recover_index.py`** 覆蓋 clean / 各類 corruption signature / `--check` 模式 / rebuild success / rebuild fail 路徑
  - **C5 `scripts/ops/win_git_escape.bat` `:done` / `:done_err` label fix + cmd-redirect pattern 文件化 + 三項 review polish**：
    - **Critical bug fix**：`win_git_escape.bat` 所有 `:do_*` handler 都 `goto :done` 或 `goto :done_err`，但這兩個 label **整個檔案都沒定義**（`:usage` 之後直接 EOF，最後一行甚至 truncate 成無換行的 `echo   `）。cmd.exe 對「goto 不存在的 label」採靜默 errorlevel=1，所以**每次成功命令都回 rc=1**，caller 永遠看到 `FAILED`。補回兩個 label（`popd` + `endlocal` + `exit /b 0/1`）、補齊 truncate 的 `:usage`、保正確 CRLF + EOF 換行。
    - **MCP PowerShell cmd-redirect pattern 文件化**：兩支 `.bat` header 加上經過 dogfood 驗證的呼叫範例。三件套：`CreateNoWindow=$true`（斷開 MCP console handle 繼承，**非這個 MCP 還是會 hang**）、`cmd.exe /s /c "..."`（`/s` 讓 cmd 乾淨地剝掉外層引號，**不是 `/c """"..."""` 那套**——實測後者會在某些 PS 引號路徑上變成 exit=0 / 0 bytes 的假通過）、`WaitForExit(ms)`（給 MCP 一個 process handle 等待，而不是讓它持有開著的 pipe）。
    - **S1 `session-init.py` `_install_commit_msg_hook` install/update 指示修正**：舊邏輯 `return "installed" if not dst.exists() else "updated"` 跑在 `dst.write_bytes()` **之後**，`dst` 永遠 exists，所以永遠回 "updated"，telemetry 的「初次安裝」事件被整個遮蔽。改為 `write_bytes` 前先 capture `existed_before = dst.exists()`。
    - **S6 `recover_index.sh` 注釋錯誤 + non-atomic write 修正**：舊注釋說 `cp (not mv) ... for atomic write behavior` — **裸 cp onto .git/index 不是 atomic**（讀者可能看到寫到一半的檔案）。改走 cp 到同 FS 的 sibling `$INDEX.recover.$$` → mv（rename(2) atomic），注釋同步更正。
    - **S7 `require_preflight_pass.sh` 注釋錯誤修正**：舊注釋說 `gh pr view with --head filter`——但指令其實是 `gh pr view <branch>`（沒用 `--head`，branch 自動對 head 分支）。改注釋，行為不變。
    - **新測試 `tests/dx/test_bat_label_integrity.py`** 7 條 parametrized assertion：每個 `goto :X` 必須有對應 `:X` label（擋 C5 bug class）、`:done` / `:done_err` 都必須存在（exit-handling contract）、header 必須含 `Process.Start` + `WaitForExit` + `CreateNoWindow` + `/s /c`（MCP caller pattern 可發現性）。
  - **C6 `scripts/session-guards/session-init.py` auto-heal git hooks**：PreToolUse hook 每次起手式時：
    - `_heal_pre_commit_shebang()` — 偵測 `.git/hooks/pre-commit` 的 shebang 指向不存在的 interpreter（典型 Windows `pre-commit install` 寫 `#!C:\Python*\python.exe` 路徑到 FUSE Linux 側不可用）→ 自動改為 `#!/usr/bin/env python3`。
    - `_install_commit_msg_hook()` — 把 `scripts/hooks/commit-msg`（C2）copy 進 `.git/hooks/commit-msg`、chmod 0o755、內容相同時 no-op、status 送進 telemetry。
    - Telemetry 新增 `hook_status: {pre_commit_shebang, commit_msg}` 欄位，和既有 session-init telemetry 合併寫 JSON Lines。所有 heal 失敗**絕不 block** session 起手式（只進 telemetry）。
  - **C7 `scripts/ops/require_preflight_pass.sh` pre-push marker 條件性啟動**：舊版任何 push 都要 `.git/.preflight-ok.<HEAD-sha>` marker，WIP iteration 階段（PR 還沒開）每次 push-to-save 都被擋、`make pr-preflight` 要跑 3-5 分鐘，是長期摩擦源。改為 state-aware：
    - `GIT_PREFLIGHT_STRICT=1` → 永遠要 marker（舊行為保留成 opt-in）
    - `gh` 不可用 → 要 marker（安全 fallback）
    - `gh pr view <branch> --json state --jq '.state'` 回 `OPEN` → 要 marker（PR 已開，CI 可見性 + reviewer noise 成本已實化）
    - `gh` 可用但無 OPEN PR → 允許 push（WIP 階段，作者自付成本）
    - **新測試 `tests/dx/test_preflight_pass_gate.py`** 15 條 parametrized 覆蓋 STRICT / 各 PR state / gh 可用與否 / multi-branch push / orthogonal bypass + main protection。特別做了 `_make_gh_missing_path(tmp_path)` helper：symlink bash/git/basename/sh/cat 到 clean dir，可靠地模擬「`gh` 不在 PATH」的 fallback 路徑。
    - **`tests/dx/test_preflight_marker.py` 既有 `blocks_*` 案例補 `env_extra={"GIT_PREFLIGHT_STRICT": "1"}`**：pin 到舊「永遠要 marker」契約，不受測試機 `gh` 可用性影響。
  - **C8 `pr_preflight.py` / `check_pr_scope_drift.py` 對非 UTF-8 git stderr 容錯**：兩個 orchestrator 的 `run()` helper 原本是 `subprocess.run(..., text=True)`（預設 UTF-8 decode）。Windows 側 git 的本地化 progress 輸出可能含 cp1252 smart-quote（0x93 / 0x94 / 0x96）等非 UTF-8 位元組，**一顆就整條 preflight 崩潰**（`UnicodeDecodeError: can't decode byte 0x93 in position 18`），連帶破壞 pre-push marker 寫入。改傳 `encoding="utf-8", errors="replace"`，stderr 本就只用於人看 / grep signature，replacement char 無害。C8 的修正 dogfood 實證：跑 `make pr-preflight` 不再被 git fetch 的 localized progress line 咬死。

- **`scripts/hooks/commit-msg`** — Conventional Commits header 本地化檢查器（installed 進 `.git/hooks/` by session-init C6）。defensive 處理：repo root 靠 `git rev-parse --show-toplevel` 解析不依賴 cwd、找不到 `pr_preflight.py` 不 block、python interpreter 多候選 PATH resolve（`python3` / `python` / 絕對路徑 fallback）。
- **`make` targets**：`fuse-commit MSG=msg.txt FILES="a b"` / `fuse-locks` / `recover-index`（對應 C3 / C3 / C4）。

### Changed

- **⚠ `da_config_defaults_change_noop_total` 語義收窄（Issue #61，v2.8.0 breaking-ish）**：原本同時涵蓋 cosmetic edit（comment-only / reorder）+ shadowed override（tenant 蓋掉變動的 default key）兩類事件；v2.8.0 起僅計 cosmetic，shadowed 案例移到新 `da_config_defaults_shadowed_total`。**Migration**：原本依賴此 counter 偵測「inheritance 機制擋下變動」的 dashboard / alert 改用新 counter；只想看 cosmetic 噪音的查詢不需改。ADR-018 §Metrics 已加 amendment。
- **`CLAUDE.md` Makefile Top 7 擴充說明**：`make win-commit` 行補充 `+ fuse-commit / recover-index` 指向 PR #44 的 FUSE 逃生門工具鏈。
- **`docs/internal/windows-mcp-playbook.md`** 新增 §FUSE Phantom Lock 防治 + §修復層 C 補 CreateNoWindow/`/s /c` 的實測 pattern（見下方 Fixed）。

- **session-init telemetry + `--stats` CLI（v2.8.0 Phase .b, PR feat/v280-session-init-telemetry）**：PR #42 事後稽核發現 — PreToolUse hook 已上線，但缺「hook 真的有跑嗎」的觀測路徑；只能靠使用者手動 `--status` 看單一 session marker，跨 session 趨勢（幾次 init / 幾次 noop / vscode_toggle 失敗率 / avg duration）完全不可見。本次把 telemetry 內建進 hook 本身：
  - **每次 hook 呼叫 append 一筆 JSON Lines**（event=`init`/`noop`/`force`；`--status` / `--stats` 是 query，刻意不寫 log 避免自我污染）。欄位：`ts` / `session_id` / `marker_digest` / `event` / `duration_ms` / `vscode_toggle`（`ok`/`partial`/`skipped`）/ `vscode_msg` / `marker_path` / `repo_root` / `pid` / `argv`
  - **Log path cross-platform 解析（4 層優先序）**：`VIBE_SESSION_LOG` env override → Windows `%LOCALAPPDATA%\vibe\session-init.log` → POSIX `$XDG_CACHE_HOME/vibe/session-init.log` → home fallback（`~/.cache/vibe/` 或 `~/AppData/Local/vibe/`）。邏輯抽成 pure `_resolve_log_path(os_name, env, home)` 可直接 unit-test，不需 monkey-patch `os.name`（後者會撞到 pathlib `WindowsPath` 無法在 Linux 實例化的 INTERNALERROR）
  - **`VIBE_SESSION_LOG=/dev/null` / `NUL` 可完全停用**（CI / 使用者 opt-out）；`_is_disabled_log_path` 提早 return、連 `mkdir` 都不跑
  - **Log 寫入失敗絕不 block**：所有 OSError 收攏、僅 stderr 印 warning、exit 0 維持不變。遵循 PreToolUse hook 的既有 never-block 原則
  - **UTF-8 safety**：`json.dumps(ensure_ascii=False)` — CJK session id / vscode_git_toggle 中文訊息原樣落盤（不 escape 成 `\uXXXX`），`jq` / 肉眼 grep 都直接可讀。dogfood 實測 vscode_git_toggle 的「✅ VS Code Git 已關閉」訊息 round-trip 乾淨
  - **`--stats` subcommand**：印 log path / size / total events / `init=N noop=N force=N` / sessions tracked / `vscode_toggle: ok=N partial=N skipped=N` / avg init duration / last N events 摘要。支援 `--limit N`（預設 10）/ `--json`（輸出原始 JSON Lines 供 `jq` pipe）/ `--session <SID>`（過濾單一 session）。Malformed log lines 自動 skip（例如寫到一半被 SIGKILL 的歪斜 line），不讓統計掛點
  - **21 新測試**（tests/dx/test_session_init.py：13 → 34）：
    - `TestTelemetryLog` × 11：env override / XDG / LOCALAPPDATA / home fallback / override-wins-on-nt-and-posix（pure function 測試，不碰 `os.name`）/ init/noop/force 事件 / partial toggle / `--status` 不寫 log / 寫入失敗 never block / `/dev/null` 停用 / CJK round-trip
    - `TestStatsCLI` × 7：empty log / summarize / `--json` mode / `--session` filter / `--limit N` / 歪斜 line skip / `--stats` 不自污染
  - **End-to-end dogfood** 已跑過 4 次 hook 呼叫（1× force + 2× noop + 1× new session init）+ `--stats --session` / `--stats --json` pipe 驗證全綠
  - **CLAUDE.md / vibe-workflow skill 同步更新**：把 `--stats` 加進手動觸發指令集、標注 log 位置與停用方式

- **Windows MCP 側 ad-hoc script 防治（v2.8.0 Phase .a, PR #41）**：延續 PR #39/#40 的 code-driven 精神，把「不要寫 throw-away `_commit.ps1` / `_pr.bat`」從文字規範升級為 L1 pre-commit hook：
  - **`scripts/tools/lint/check_ad_hoc_git_scripts.py`**（pre-commit 硬失敗，whitelist 模式）：掃 repo 內所有 `*.bat` / `*.ps1` / `*.cmd`，不在 `scripts/ops/` / `scripts/tools/` / `tools/` allowlist 中即 fail。用 whitelist 而非 blacklist regex 的理由：PR #40 session 寫了 `_p40_commit.ps1` / `_p40_pr.bat` / `_p40_checks.bat` / `_p40_failog.bat` / `_p40_diag.bat` 五隻 script，黑名單追不上每個新動詞（check / failog / diag），whitelist 強制所有新 wrapper 走 PR review。
  - **`scripts/ops/win_gh.bat`**（v2.8.0 新增）：GitHub CLI 的 MCP-friendly wrapper。Desktop Commander PowerShell 下 `"C:\Program Files\GitHub CLI\gh.exe"` 的引號會被多層 escape 破壞；`win_gh.bat` 改用 8.3 short path `C:\PROGRA~1\GITHUB~1\gh.exe`、強制 `PATHEXT` + `PATH` 含 `Git\cmd`、全 ASCII 註解、CRLF line endings。子命令：`pr-checks [PR#]` / `pr-view [PR#]` / `pr-create <flags>` / `run-view <RUN_ID>` / `run-log <RUN_ID>` / `raw <args>`（逃生門）。取代 session 每次自己寫 `_pr_checks.bat` 的循環。
  - **`docs/internal/windows-mcp-playbook.md §修復層 C` 重寫**：逃生門工具表新增 `win_gh.bat`；opening 改為「⛔⛔⛔ 鐵則」並 chronicle PR #39 / #40 的 1 + 5 = 6 支 ad-hoc script。
  - **`docs/internal/windows-mcp-playbook.md` 新增 §MCP Shell Pitfalls 節**：編寫 `.bat` / `.ps1` wrapper 必讀的 4 雷清單（Short path / CRLF / ASCII-only / `PATHEXT`+`PATH` 雙設）+ 起手式模板 + 3-step 自測 one-liner。
  - **新增 LL #54 + #55 + #56**：#54 chronicle PR #39 / #40 ad-hoc script proliferation；#55 記錄 `win_gh.bat` 初次實作踩到的 short-path / CRLF / PATH 三件套；**#56** 記錄 PR #41 dogfood 本身觸發的兩個二次踩坑——(a) `win_gh.bat` / `win_git_escape.bat` 實作時忘了 `set PATHEXT`，撞到使用者 profile 的 `PATHEXT=.CPL` 直接 break gh 內部 git 呼叫；(b) PR #41 base 堆在 PR #40 還未 squash-merge 的分支上，main squash 後 PR #41 進入 `mergeStateStatus: DIRTY`，GH 靜默跳過 `on: pull_request` CI（零 workflow 觸發）。對應修正：兩個 wrapper 都加 `set "PATHEXT=.COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"`、`feat/p41-codify-windows-commit` rebase 到 origin/main 去除重複 commits。
  - **`.gitignore` 政策翻轉**：移除 `_*.bat` / `_*.ps1` / `_*.txt` / `_*.md` scratch-hiding patterns（保留 `_*.json` / `_*.out` / `_*.err` / `_ci_logs/` 等真正 unreviewable artifact）。理由：「藏起來」讓 cleanup 步驟不可見，下個 session 看不到前一個 session 的垃圾，每次重寫。改走 **adopt-or-delete** 路線——scratch 要不升格進 `scripts/ops/`、要不 commit 前刪乾淨。
  - **`CLAUDE.md` Top 6 坑 → Top 7 坑**：新增 §6「⛔ 絕對不要寫 `_foo.bat` / `_p*_commit.ps1`」指向 `win_gh.bat` / `win_git_escape.bat`。

- **`dev-rules.md §P2` 轉 code-driven enforcement（v2.8.0 Phase .a）**：PR #39/#40 踩坑後的結構性改進——純文字規範（「`gh pr create` 前記得掃 drift」）對 agent 記性依賴過高，改寫為兩個 hook：
  - **`scripts/tools/lint/check_devrules_size.py`**（pre-commit 硬上限）：`docs/internal/dev-rules.md` 超過 **500 行**即 fail。用意是把 dev-rules.md 的累積量做為「code-driven 遷移壓力反向指標」——文字規範越肥代表越多條目本來就該當 hook 寫掉。新增規則時作者被迫三選一：prune / promote（升 L1/L2 hook）/ archive。放寬 `MAX_LINES` 屬禁忌，需在 PR body 明述理由。
  - **`scripts/tools/lint/check_pr_scope_drift.py`**（pr-preflight 硬失敗）：偵測兩項——tool-map drift（`generate_tool_map.py --check` 失敗，典型肇因：新增 `scripts/tools/**/*.py` 但沒 regen）+ working-tree dirty（unstaged / uncommitted staged 存在，典型肇因：session 邊改 playbook / CLAUDE.md 忘記 git add）。
  - **`scripts/tools/dx/pr_preflight.py` 新增 Scope drift phase**：`make pr-preflight` 從 6 項 → **7 項**檢查（branch / behind-main / conflict / local hooks / **scope-drift** / CI / mergeable）。PR merge 前必過。
  - **`dev-rules.md §P2` 從文字敘述改為 hook pointer**：規則本體即 code，避免「文字規範 → 記性 → 執行」三段 rot。新增 drift 項目時改 code，不改本節。

### Changed

- **`.pre-commit-config.yaml` 新增 `devrules-size-check` hook**：緊鄰 `tool-map-check`，僅在 `docs/internal/dev-rules.md` 變動時觸發。
- **`Makefile` `pr-preflight` target 描述更新**：反映 7 項檢查範圍（含 scope-drift）。
- **`dev-rules.md` 大幅瘦身（v2.8.0 Phase .a）**：520 → 487 行，為新 500-line cap 留 buffer。壓縮 §S3 / §S5 的反例+正例 block（資訊保留、用註解合併）。未刪任何規則條文。
- **CLAUDE.md tool count `114 → 117`**：同步 `docs/internal/tool-map.md` regenerated 計數（ops 46 / dx 29 / lint 41，累積 +3：PR #40 的 `check_devrules_size.py` + `check_pr_scope_drift.py`，PR #41 的 `check_ad_hoc_git_scripts.py`）。`make pr-preflight` 描述同步更新為 7 項。

- **Phase .a fresh-eye review trim — playbook / dev-rules 收斂到 codified automation（v2.8.0）**：fresh-eye review 對照 §3 軌道二的 codify→slim 循環產出，發現 4 個 trim 目標僅 3 達標；補完欠帳並擴大到相鄰文字規範。Net `−153 lines / +33 lines` across 4 docs，無 rule 條文遺失，每處皆有 `✅ Codified` 指向對應 hook / make target。
  - **`windows-mcp-playbook.md` §預防層 1 VS Code Git 開關（−12 行）**：`session-init.py` PreToolUse hook 已 first-tool-call 自動 invoke `vscode_git_toggle off`，原 16 行段壓成 5 行說明 + 3 行手動 fallback；移除冗餘「⚠️ Agent 起手式」reminder（重複 CLAUDE.md）。
  - **`windows-mcp-playbook.md` §修復層 B Level 1–5（−40 行）**：53 行逐層敘述 → 6 行表格 + 1 行決策樹；每層原由 + 設計脈絡搬到 `docs/internal/archive/automation-origins/fuse-cache-recovery.md`（codified-as: `make fuse-reset` + `make session-cleanup`）。
  - **`windows-mcp-playbook.md` §修復層 B Level 6 rename-trick（−32 行）**：被 Trap #44「phantom 薛丁格態唯一可靠解法 = `win_git_escape.bat` 走 Windows 原生 git」+ PR #44 plumbing 逃生門（`fuse_plumbing_commit.py` / `recover_index.sh`）取代；2026-04-10 case study + Python 範例搬到 `docs/internal/archive/automation-origins/fuse-rename-trick.md`。
  - **`windows-mcp-playbook.md` Trap #57（status flip）**：`pre-commit head-blob-hygiene` 0-output 卡死從「短/中/長期處置」散文補上 **🟡 Mitigated（v2.8.0）** 標頭 + 自動清理路徑 `make fuse-reset` + 復原路徑 `make commit-bypass-hh` + S#31 發現的 `~/.cache/pre-commit/patch{TS}-{PID}` recovery vector；對齊 #58/#59/#60/#61 的 status marker 風格。
  - **`testing-playbook.md` §v2.7.0 LL Section 2（test.fixme 治理，−6 行）**：bare `test.fixme()` / `test.skip()` 三條規則從敘述體翻成「✅ Codified（PR #57）」單段 + 兩條 lint 不管的人類判斷項（登記義務 / calibration sprint trigger）。`eslint-plugin-playwright/no-skipped-test` `{ allowConditional: false, disallowFixme: true }` 已 commit-time 擋下。
  - **`github-release-playbook.md` Pitfall #25 BOM（status flip）**：從「`Out-File -Encoding utf8` 都會加 BOM」散文 → **✅ Codified（PR #56）** 指向 `pr_preflight.py::detect_commit_msg_bom()` + Trap #61 archive，與 windows-mcp-playbook Pitfall #61 對齊。
  - **`dev-rules.md` §11 + §12（−54 行）**：§11 「`sed -i` 三條應該用 + 自動修復 + symlink LL 提醒」壓成 1 段規則 + 1 段 ✅ Codified 指向 `file-hygiene` hook；§12 「Branch + PR 規則 + Harness 安裝 + 7 項 SOP 表 + 4 個 status × 4 row × 3 入口」壓成 1 段規則 + 1 段 ✅ Codified 條列（`protect_main_push.sh` / `make pr-preflight` / `require_preflight_pass.sh` / 7 項清單）+ 1 行三入口 + 1 行指向 github-release-playbook 細節。500-line cap headroom 從 13 行擴大到 ~67 行。
  - **新檔**：`docs/internal/archive/automation-origins/fuse-cache-recovery.md` + `fuse-rename-trick.md`（兩檔皆含 frontmatter `codified-as` / `original-playbook` / `codified-at-version: v2.8.0` / `status: archived`）。Archive 結構從 2 檔（trap-60 / trap-61）擴到 4 檔。**注**：PR #73 已將 `doc-map.md` 範圍收窄到 public docs only（exclude `docs/internal/**`），故新 archive 兩檔不會出現在 doc-map；無 doc-map regen 連動需求。
  - **驗收**：planning §3 軌道二 trim 目標 — CLAUDE.md「6 坑」✅、benchmark-playbook stdout 段 ✅、testing-playbook Playwright fixme 段 ✅、windows-mcp-playbook FUSE 段 ✅（從未達標 → 達標）。Phase .e 不再需要補欠 trim 工作。

### Fixed

- **A-5a `make pr-preflight` / `pre-tag` Chart.yaml path bug（Phase .a A-5a）**：Helm chart 從 `components/threshold-exporter/` 遷至 `helm/threshold-exporter/`（parallels `helm/tenant-api/`）時遺漏 3 處 stale reference，導致 `make version-check` 印 `grep: components/threshold-exporter/Chart.yaml: No such file or directory` warning、`make chart-package` / `chart-push` target 失效：
  - `Makefile:475` — `CHART_DIR := components/threshold-exporter` → `helm/threshold-exporter`
  - `scripts/tools/dx/bump_docs.py:66` — `CHART_YAML = REPO_ROOT / "components" / "threshold-exporter" / "Chart.yaml"` → `"helm" / "threshold-exporter"`（line 68 `TENANT_API_CHART_YAML` 早已用 `helm/` 為正確範式）
  - `docs/internal/github-release-playbook.md:397` — release step 15 驗證指令 grep path 同步修正
  - 驗證：`make version-check` 輸出不再含該 warning；Helm chart 發佈主路徑恢復可用

### Changed

- **Doc governance — Planning Artifact Policy 升 SSOT + Session Ledger 退場（Phase .a Session #18）**：v2.8.0 期間反覆觸發 context-compact 的根因分析催生兩項規範化動作。原 `v2.8.0-planning.md §12.6` 的「Planning Artifact Policy」（L1/L2/L3 文件分類 / 決策樹 / retention rule）跨版長期原則升至 `dev-rules.md §A 產出物治理`（`docs/internal/dev-rules.md`）作 SSOT；新增 §A6「v2.9.0+ planning doc 不再保留 §12.1 Session Ledger」（compact-pressure 主因為 append-only Session 表 row 動輒 2-4 KB）。
  - **新規範**：`dev-rules.md §A1-A7`（taxonomy / 4 條為何不落 repo / pattern 清冊 / 決策樹 / retention rule / Session Ledger 退場 / dissent pointer）
  - **新工具**：`scripts/tools/lint/validate_planning_session_row.py`（manual-stage：偵測 §12.1 Session row 超過 char limit，預設 2000）
  - **新 Makefile target**：`make check-planning-bloat`（直接呼叫上述 lint）+ `make session-cleanup` 末尾自動跑（best-effort，不阻擋 cleanup）
  - **既有 planning.md 瘦身**（Session #18 同步執行）：`v2.8.0-planning.md` 從 ~987 lines → 720 lines；§12.3.1 Q1-Q6 詳細分析 / §12.4 resolved-trap RCA（#3 #9 #10 #11 #12）/ §12.6 政策 dissent / §12.7 PR#1 完整 mapping table 全搬至 `v2.8.0-planning-archive.md`（gitignored，maintainer-local）；§12.5 Active TODO 從 ~15 mixed `[x]/[ ]` 收斂為 5 純 active 項
  - **CLAUDE.md / 其他 SSOT 不變**：本次遷移為 internal 文件治理，不影響 user-facing API / schema / CLI

- **A-5b scan_component_health `status: archived` opt-in 下架路徑（Phase .a A-5b）**：`scripts/tools/dx/scan_component_health.py` 擴展 Tier policy 以支援 registry 手動標記下架，非破壞性（Q2 warning-only 政策延伸）：
  - **Registry schema 擴展**：`docs/assets/tool-registry.yaml` 工具項可新增 `status: archived` + `archived_reason: "..."`（本版未標任何工具 archived，schema + 邏輯擴展為主）
  - **scan 行為**：archived 工具產出 `tier: "Archived"` / `status: "ARCHIVED"`，保留 LOC / i18n 作 visibility metric，**從所有 aggregates 排除**（`tier_distribution` / `token_group_distribution` / `playwright_coverage` / `i18n_coverage_distribution` / `tools_with_hardcoded_hex` / `tools_with_hardcoded_px` / `tier1_token_group_a/c`）—— aggregates 分母統一改用 `active_results`（`status == "OK"`）
  - **Summary 新增 4 欄位**：`total_active_tools`（未 archived 計數）、`archived_count`、`archived_tools: [keys]`、`archive_candidates: [{key, reason}]`（自動建議清單供 PR review）
  - **自動建議 criteria（6 條 AND）**：`tier == "Tier 3 (deprecation_candidate)"` AND `loc < 50` AND `tier_breakdown.recency == -1`（>180 天未動）AND `tier_breakdown.writer == 0` AND `not playwright_spec` AND `first_commit > 365 天`。過於保守（防 false positive）—— 最終下架決策仍需維護者寫入 registry
  - **新測試**：`tests/dx/test_scan_component_health.py`（12 cases，`_is_archive_candidate` × 8 threshold 測試 + `scan()` × 4 integration，tmp_path + monkeypatch 完全隔離 git/jsx 依賴）
  - **dev-rules.md 新增 §T 工具生命週期**：四態轉換表（active / deprecation_candidate / archive_candidate / archived）+ 判定來源 + scan_component_health 行為 + opt-in rationale + 排除後仍保留 LOC/i18n 的原因（避免 archived 工具在 registry 中完全消失）

- **TECH-DEBT-007 resolved — `--da-color-hero-muted` contrast fail via token-split**（v2.8.0 Phase .a PR#1c）：修復 multi-tenant-comparison / dependency-graph 在 light bg 下 axe-core `color-contrast` 40-node 違規。根因並非 token 色值單純「太淺」，而是**單一 semantic token 被迫服務兩種亮度相差 > 40% 的背景**（hero dark `#0f172a` + tile light `hsl(x,60%,90%)` / SVG white）——任何單值都無法同時滿足 WCAG AA 4.5:1。
  - `docs/assets/design-tokens.css`：保留 `--da-color-hero-muted: #94a3b8`（hero dark bg 7.2:1 AA pass）；新增 `--da-color-tile-muted: #6b7280`（white 4.83:1 AA pass），light / dark mode 皆同值（consumer 背景不翻色）
  - `docs/interactive/tools/multi-tenant-comparison.jsx` L194 `defaultBadgeStyle`：`hero-muted` → `tile-muted`（HeatmapRow cell badge 處於 `hsl(hue,60%,90%)` 永遠亮底）
  - `docs/interactive/tools/dependency-graph.jsx` L215：SVG `<text fill>` `hero-muted` → `tile-muted`（parent `bg-white` SVG 容器）
  - L133 `MetricCard subStyle` **刻意排除在 PR#1c scope 外**：card bg 隨 `[data-theme="dark"]` 翻色（light `#f8fafc` ↔ dark `#334155`），需 theme-aware override 另案處理 → 登錄 TECH-DEBT-016 追蹤
  - 新增 `dev-rules.md §S5 單一 semantic token 不可 serve 亮度相差 > 40% 的兩種背景`：固化 token-split 規則 + 命名慣例（`--da-color-<surface>-<intent>`）+ 雙主題翻色 caveat
  - `known-regressions.md`：TECH-DEBT-007 狀態 open → **resolved**（附 fixed_in 引述本 PR）；TECH-DEBT-016 新登錄
  - 背景分析：plan.md §12.4 Trap #10（shared-token-across-opposing-backgrounds 反模式）、§12.5 PR#1c spec

- **Blast Radius PR comment length guard**（v2.7.0 defensive patch）：`scripts/tools/ops/blast_radius.py` `generate_pr_comment` 加三層守門，避免 GitHub 65,536 char 硬上限造成 CI 靜默失敗（422 Unprocessable Entity，bot 會「成功」但 comment 不存在）：
  - 當 Tier A+B affected tenant > 50 時，切 **summary-only mode**（只列 tenant IDs，不展 per-field diff；完整 diff 走 `blast-radius-report` workflow artifact）
  - Summary-only mode 內部再 cap 200 條，多的收斂為「+N more」
  - 60,000 char safety limit：即使 fell-through（例如單一 tenant 有上千欄位變動），也會 auto-fallback 到 summary-only 或最後手段硬截斷
  - 新增 `--artifact-hint` CLI flag，`.github/workflows/blast-radius.yml` 對應 pass workflow run URL，讓 reviewer 看 summary-only comment 時能一鍵跳去 artifact
  - 9 tests 於 `tests/ops/test_blast_radius.py::TestPRCommentLengthGuard`：1000-tenant、超長 field diff、Tier C count-only、artifact hint rendering 等情境均驗證 output 長度 < 65,536
  - 發現來源：Gemini R2 cross-review；實測 1000-tenant Tier A 場景原輸出 ~260 KB，超限 4 倍



## [v2.7.0] — 千租戶配置架構 + 元件健壯化 (2026-04-19)

v2.7.0 把租戶配置的資料結構升級為可支撐千租戶規模（`conf.d/` 階層 + `_defaults.yaml` 繼承引擎 + dual-hash 熱重載），把 v2.6.0 的 Design Token 定義推進到全面採用，並把測試與 CI 從「能跑」升級為「可規模化」。

### Scale Foundation I — 千租戶配置架構（ADR-017 / ADR-018）

- **`conf.d/<domain>/<region>/<env>/` 階層目錄**：任一層可放 `_defaults.yaml`，`L0 defaults -> L1 domain -> L2 region -> L3 tenant` 四層 deep merge，array replace / null-as-delete 語義明確
- **Dual-hash 熱重載**：`source_hash`（原始檔 SHA-256）+ `merged_hash`（canonical JSON SHA-256）並行追蹤，merged_hash 變才 reload；300ms debounce 吸收 K8s ConfigMap symlink rotation 的連續寫入
- **Mixed-mode**：舊扁平 `tenants/*.yaml` 與新 `conf.d/` 可共存，無強制一次遷移
- **`GET /api/v1/tenants/{id}/effective`**：回傳 merged config + 繼承鏈 + dual hashes，方便 debug 實際生效設定
- **新 CLI**：`da-tools describe-tenant`（含 `--what-if <file>` 模擬 `_defaults.yaml` 變動 -> diff merged_hash）+ `da-tools migrate-conf-d`（扁平 -> 階層自動 `git mv`，預設 `--dry-run`）
- **Schema 新增**：`tenant-config.schema.json` 加入 `definitions/defaultsConfig` + `_metadata.$comment`

### 元件健壯化

- **Design Token 全面遷移**：9 個 Tier 1 JSX 工具完成 Tailwind -> arbitrary value token 改寫（`wizard` / `deployment-wizard` / `alert-timeline` / `dependency-graph` / `config-lint` / `rbac` / `cicd-setup-wizard` / `tenant-manager` / `multi-tenant-comparison`）；剩餘 7 個 px-only 工具延 v2.8.0
- **`[data-theme]` 單軌 dark mode**（ADR-016）：移除 Tailwind `dark:` 雙軌橋接，解決 v2.6.0 誤用陷阱
- **Component Health Snapshot**（ADR-013）：`scan_component_health.py` 五維評分（LOC / Audience / Phase / Writer / Recency）-> Tier 1 = 11 / Tier 2 = 25 / Tier 3 = 3；新增 `token_density` 量化 token 採用進度
- **Colorblind 合規**（ADR-012）：`threshold-heatmap` 結構化 severity（不只靠顏色）
- **TECH-DEBT 類別獨立 budget**（ADR-014）：從 REG budget 分出，不佔 REG P2/P3 配額
- **新 lint**：`check_aria_references.py` / `axe_lite_static.py` / `check_design_token_usage.py`

### 測試與基礎設施

- **`tests/` 子目錄分層**：`dx/` / `ops/` / `lint/` / `shared/`，匹配 `scripts/tools/` 的分層
- **1000-tenant synthetic fixture**：`generate_synthetic_tenants.py` 產可重現的千租戶資料，供 B-1 Scale Gate 量測
- **Blast Radius CI bot**：PR 變更自動計算影響的 tenants / rules / thresholds，comment 到 PR
- **Pre-commit**：31 auto + 13 manual-stage；`make pre-tag` 整合 `version-check` + `lint-docs` + `playbook-freshness-ll`

### Benchmark（1000 tenants, Intel Core 7 240H, Go 1.26.1, `-benchtime=3s -count=3`）

| 指標 | 時間 | 語義 |
|:---|---:|:---|
| `FullDirLoad_1000` | 112 ms | Cold load（scan + YAML parse + merge + hash） |
| `IncrementalLoad_1000_NoChange` | 2.45 ms | Dual-hash reload noop（45x 快於 cold） |
| `IncrementalLoad + MtimeGuard` | 1.30 ms | 加 mtime 短路（86x 快於 cold） |
| `MergePartialConfigs_1000` | 653 us | 階層 merge 本身 |

SLO：cold load 112 ms / 1000 tenants；reload 熱路徑 1.30 ms 相對於預設 15 s scan_interval 僅 0.0087%，幾乎零 overhead。完整報告見 [`benchmarks.md §12`](docs/benchmarks.md#12-incremental-hot-reload-b-1-scale-gate)。

### ADR 新增（ADR-012~018，7 條）

colorblind 結構化 severity / component health + token_density / TECH-DEBT 獨立 budget / token 遷移策略 / 單軌 dark mode / `conf.d/` 階層 / `_defaults.yaml` 繼承引擎 + dual-hash 熱重載。

### Breaking changes

無。`conf.d/` 與繼承引擎為**新增能力**；舊扁平 `tenants/*.yaml` 完全向後相容，Schema 只新增不改動。

### Upgrade notes

- 既有使用者：不需變更
- 想採用 `conf.d/` 分層：見 `docs/scenarios/multi-domain-conf-layout.md` + `incremental-migration-playbook.md`，或 `da-tools migrate-conf-d --dry-run`
- 熱重載：dual-hash 預設啟用，debounce window 300ms 可用 `--scan-debounce` 調整

---

## [v2.6.0] — Operator 遷移路徑 × PR Write-back × 設計系統統一 (2026-04-07)

v2.6.0 的核心是「讓 enterprise 客戶能信賴地在 Operator 環境下運營」：建立完整的 ConfigMap → Operator 遷移工具鏈與對稱文件（ADR-008 addendum），引入 PR-based 非同步寫入支援 GitHub 與 GitLab 雙平台（ADR-011），統一設計系統消除三套平行 CSS 的技術債，並新增 4 個互動工具強化價值傳達。

### K8s Operator 完整遷移路徑

v2.3.0 引入的 Operator 指南是單一文件；v2.6.0 將其擴展為與 ConfigMap 路徑完全對稱的文件體系與工具鏈。

- **ADR-008 addendum**：正式記錄架構邊界宣言——threshold-exporter 不 watch 任何 CRD，CRD → conf.d/ 轉換由外部控制器或 CI 負責。含 Mermaid 邊界圖 + 三問判斷標準（ZH + EN 雙語）
- **`operator-generate` 大幅增強**：AlertmanagerConfig 6 種 receiver 模板（Slack, PagerDuty, Email, Teams, OpsGenie, Webhook），每種自動產出 `secretKeyRef` 引用 K8s Secret（零明文 credential）。新增 `--receiver-template`、`--secret-name`、`--secret-key` 參數
- **三態抑制規則 CRD 化**：Silent / Maintenance mode 自動包含在每個 AlertmanagerConfig 產出（4 條 inhibit rules）
- **Helm `rules.mode` toggle**：threshold-exporter chart 新增 `configmap | operator` 切換 + ServiceMonitor 條件模板，operator section 含 ruleLabels、serviceMonitor、receiverTemplate、secretRef
- **`da-tools migrate-to-operator`**（新增 CLI）：讀取現有 ConfigMap rules → 產出等效 CRD + 6 階段遷移清單（Discovery → Generate → Shadow → Compare → Switch → Cleanup）+ rollback 程序。`validate_tenant_name()` RFC 1123 驗證確保 CRD apply 不失敗
- **Operator Setup Wizard**（新增 JSX）：互動式偵測環境 → 選 CRD 類型 → 產出命令，每步驟含 contextual help + 常見陷阱提示
- **Kustomization.yaml 自動產生**：`operator-generate --kustomize` 產出標準格式，含 commonLabels + sorted resources + namespace
- **`drift_detect.py` Operator 模式**：`--mode operator` 透過 kubectl 取得 PrometheusRule CRD 的 spec.groups SHA-256，與本地 YAML 比對。kubectl timeout 30s + 三種錯誤處理
- **Decision Matrix**：提升到 Getting Started 層級，決策樹 + 10 維度比較表（ZH + EN）
- **文件對稱化**：`prometheus-operator-integration.md` 拆分為 4 組子文件（Prometheus / Alertmanager / GitOps / Shadow Monitoring）各含 ZH + EN 版本 + 2 hub 導航頁 = 10 篇新文件

### PR-based Write-back + 非同步 API

v2.5.0 的 tenant-api 只支援 direct write（API → YAML → git commit）。v2.6.0 新增 PR 模式與非同步批量操作，讓高安全環境能透過 code review 流程管理配置變更。

- **ADR-011**（新增 ADR，ZH + EN 雙語）：定調 PR lifecycle state model（pending / merged / conflicted）、GitHub PAT 權限與 Secret 管理策略、多 PR 合併衝突處理、eventual consistency 語義
- **PR-based write-back**：`_write_mode: direct | pr` 配置切換（`-write-mode` flag + `TA_WRITE_MODE` env）。UI 操作 → 建立 PR → reviewer 核准 → 合併。PR-mode API response 回傳 `pr_url` + `status: "pending_review"`
- **Batch PR 合併**：群組批量操作合併為單一 PR（非 N 個），減少 reviewer 負擔
- **Async batch operations**：`?async=true` query param 啟用非同步模式，回傳 `task_id` + `status: "pending"`。goroutine pool 執行，GET `/tasks/{id}` polling 查詢進度
- **Orphaned task 容錯**：Pod 重啟後 in-memory task state 遺失，GET `/tasks/{id}` 回傳 404 附帶 `pod_may_have_restarted` hint
- **SSE 即時通知**：`GET /api/v1/events` 端點，gitops.Writer 寫入成功後自動推播 `config_change` 事件。採用 Server-Sent Events 實作，零外部依賴
- **tenant-manager.jsx**：Pending PRs 提示 banner（頂部顯示待審核 PR 數量與連結，30s 輪詢）

### Platform Abstraction Layer + GitLab 支援

為使 PR write-back 成為平台無關的能力，抽取 platform interface 並新增 GitLab MR 支援。

- **`internal/platform/platform.go`**（新增）：`PRInfo` struct、`Client` interface（5 methods: CreateBranch / CreatePR / ListOpenPRs / ValidateToken / DeleteBranch）、`Tracker` interface（6 methods）。handler 只依賴 interface，provider 可替換
- **`internal/gitlab/`**（新增套件）：GitLab REST API v4 client，`PRIVATE-TOKEN` header 認證，`url.PathEscape` 支援含 `/` 的 `group/subgroup/project` 路徑。全部 5 個 `platform.Client` 方法 + 6 個 `platform.Tracker` 方法
- **Write mode 路由**：`--write-mode direct | pr | pr-github | pr-gitlab` 四種模式，`pr` 為 `pr-github` alias（向後相容）
- **On-Premise 支援**：GitHub Enterprise Server（`TA_GITHUB_API_URL`）+ 自託管 GitLab（`TA_GITLAB_API_URL`）。`SetBaseURL()` 已納入 `platform.Client` interface
- **Compile-time interface assertions**：`var _ platform.Client = (*Client)(nil)` + `var _ platform.Tracker = (*Tracker)(nil)` 確保型別安全
- **錯誤訊息衛生化**：`doRequest` 在 HTTP 4xx/5xx 時 log 完整 response body（debugging），回傳 caller 的 error 只含 status code（不洩漏 API body 給前端）。GitHub + GitLab 兩端一致
- **GitLab state 正規化**：`normalizeState()` 將 GitLab `opened` 映射為 `open`（與 GitHub 一致）
- **ListOpenPRs pagination**：per_page=100, 10 pages safety limit

### 設計系統統一

v2.5.0 暴露了三套平行 CSS 系統（CSS variables / Tailwind / inline styles）是所有無障礙問題的根源。v2.6.0 建立 design token SSOT 並全面遷移。

- **`docs/assets/design-tokens.css`**（新增）：統一 CSS variable 定義（11 個類別：color, spacing, typography, shadow, radius, transition 等），按 §1-§11 組織，命名規範 `--da-{category}-{element}-{modifier}`
- **Dark mode 三態切換**：`[data-theme="dark"]` attribute 取代 `@media (prefers-color-scheme: dark)`。Portal 加入 Light / Dark / System 三態切換按鈕，狀態存 localStorage（fallback: in-memory + cookie）
- **tenant-manager.jsx 遷移**：消除 454 行 hardcoded inline styles，全面切換至 CSS variables + Tailwind classes
- **focus-visible 全局化**：CSS 層統一實作，不再依賴各 JSX 檔案自行加入
- **index.html 統一**：legacy aliases（`var(--bg)`, `var(--muted)`）全面遷移至 `var(--da-*)` tokens
- **`docs/internal/design-system-guide.md`**（新增）：design token 命名規範、使用方式、`[data-theme]` 切換機制、Light/Dark/System 三態邏輯

### 價值傳達與互動工具

讓潛在使用者與現有客戶能快速量化平台的採用價值。

- **ROI Calculator 增強**：新增 Quick Estimate 模式（單一輸入即出結果）+ 完整三維計算（Rule Maintenance + Alert Storm + Time-to-Market）
- **Migration ROI Calculator**（新增 JSX）：輸入 PromQL 行數 / rules / tenants → coverage estimation + migration effort + break-even analysis
- **Cost Estimator**（新增 JSX, 827 lines）：tenants × packs × scrape interval × retention × HA replicas × deployment mode → Resource Summary + Monthly Cost + ConfigMap vs Operator 比較 + Quick Recommendation
- **Notification Template Editor**（大幅改版, 897 lines）：從 Previewer 升級為 Editor——可編輯 title/body 模板 + template variable autocomplete + validation（unmatched braces, char limits）+ live preview + export YAML/JSON + template gallery（Detailed/Compact/Bilingual presets）
- **architecture-and-design.md** 每個子主題加入 business impact 欄位（ZH + EN，O(M) vs O(N×M) 複雜度對比、Onboard 2hr→5min 等量化指標）
- **release-notes-generator.jsx** 新增 `generateAutoSummary()` 函式，CHANGELOG 角色分流自動摘要（per-role "What's new for you"）

### 測試與品質

- **Playwright axe-core 整合**：`@axe-core/playwright` 自動偵測 WCAG 違規，整合到既有 5 個 smoke tests + 新增 Operator Wizard 12 tests
- **Property-based testing**（新增 22 tests）：Hypothesis 覆蓋 tenant name RFC 1123 validation、SHA-256 hashing、drift detection symmetry、YAML round-trip、kustomization builder。`@settings(max_examples=100)` 確保覆蓋
- **Go `-race` 全通過**：Phase .e 發現並修復 async/taskmanager.go + ws/hub_test.go data race。`Get()` 改為回傳 deep copy snapshot 防止併發讀寫
- **大型 Python 工具重構**：`generate_alertmanager_routes.py`（1,474→1,645 lines，21 helpers extracted，>100 行函式 4→0）+ `init_project.py`（1,404→1,438 lines，6 helpers extracted）
- **aria-live regions**：tenant-manager.jsx 新增 4 個 region（sidebar, PRs banner, batch, tenant grid）+ threshold-heatmap.jsx 新增 3 個 region
- **Batch response summary**：tenant_batch.go + group_batch.go 回傳 `summary` 欄位（"N succeeded, M failed"）
- **version-consistency hook 擴展**：覆蓋 e2e/package.json、JSX 工具版號
- **tool-registry.yaml 對齊**：補齊 3 個缺失條目（rbac-setup-wizard, release-notes-generator, threshold-heatmap）

### 數字

| 項目 | v2.5.0 | v2.6.0 | 變化 |
|------|--------|--------|------|
| JSX 互動工具 | 38 | 42 | +4 |
| ADRs | 10 | 11（+ ADR-011）+ ADR-008 addendum | +1 |
| Operator 子文件 | 1 | 10（4 ZH + 4 EN + 2 hub） | +9 |
| Go test packages（`-race` clean） | — | 11 packages, 0 race | NEW |
| Property-based tests (Hypothesis) | 0 | 22 | NEW |
| Helm chart features | — | `rules.mode` toggle + ServiceMonitor | NEW |
| Write-back 模式 | 1（direct） | 4（direct / pr / pr-github / pr-gitlab） | +3 |
| Platform providers | 0 | 2（GitHub + GitLab） | NEW |
| Python 工具 | 91 | 95 | +4 |
| Pre-commit hooks | 19 auto + 9 manual | 19 auto + 10 manual | +1 manual |
| 環境變數（tenant-api） | ~10 | ~18 | +8（Write-back + GitLab） |

### 🐛 Bug Fixes

- `migrate_to_operator.py`：`discover_tenant_configs()` 靜默過濾無效 tenant 名稱 → 改為回報至 `analysis["issues"]` 清單
- `tracker.go`：`RegisterPR()` 同 tenant 可能重複 append → 改為 replace-or-append 邏輯
- `migration-roi-calculator.jsx`：2 個 label 未翻譯
- index.html：light-mode `.journey-phase-badge` + `.card-icon` 殘留 hardcoded hex color → 全部改用 design tokens
- README.md / README.en.md：badge 版號 v2.5.0 → v2.6.0
- troubleshooting.en.md：缺少 Prometheus Operator 章節 → 新增完整診斷+修正步驟+Rollback 程序
- troubleshooting.md：Operator 章節僅有診斷 → 補充三種修正步驟 + Rollback 程序

---

## [v2.5.0] — Multi-Tenant Grouping × Saved Views × E2E Testing (2026-04-06)

v2.5.0 在 v2.4.0 建立的 Tenant API 基礎上，實現租戶分群管理（ADR-010）、Saved Views、Playwright E2E 測試基礎，並新增 4 個互動工具。

### Multi-Tenant Grouping（ADR-010）

- 新增 `conf.d/_groups.yaml` 儲存結構：靜態 `members[]` 成員清單，Git 版本化，可 code review
- Group CRUD API：`GET/PUT/DELETE /api/v1/groups/{id}` + `POST /api/v1/groups/{id}/batch` 批量操作
- Permission-filtered listing：ListGroups 只回傳使用者有權限存取至少一個成員的 group
- 批量操作逐 tenant 驗證寫入權限，部分失敗不影響已成功項目

### Saved Views API

- 新增 `conf.d/_views.yaml`：持久化篩選條件（environment + domain + 自訂 filter 組合）
- CRUD 端點：`GET/PUT/DELETE /api/v1/views/{id}`，支援使用者自建常用視圖
- 與 Portal tenant-manager 整合：一鍵切換預設篩選

### Tenant Metadata 擴展

- 新增可選欄位：`environment`、`region`、`domain`、`db_type`、`tags[]`、`groups[]`
- 全部向後相容——未設定 metadata 的 tenant 不受影響
- Metadata 僅 API/UI 層使用，不影響 Prometheus metric cardinality

### RBAC 增強

- `_rbac.yaml` 新增 `environments[]` 和 `domains[]` 可選過濾欄位
- 支援「特定 group 只能管理 production 環境」等細粒度控制

### 新增互動工具（34 → 38 JSX tools）

- **Deployment Profile Wizard** (`deployment-wizard.jsx`)：互動式 Helm values 產生器
- **RBAC Setup Wizard** (`rbac-setup-wizard.jsx`)：互動式 `_rbac.yaml` 產生
- **Release Notes Generator** (`release-notes-generator.jsx`)：從 CHANGELOG 自動產生角色導向更新摘要
- **Threshold Heatmap** (`threshold-heatmap.jsx`)：跨 tenant 閾值分佈熱力圖 + 離群偵測 + CSV 匯出

### Playwright E2E 測試基礎

- 5 個 critical path spec（38 個 test case）：portal-home、tenant-manager、group-management、auth-flow、batch-operations
- Mock API 隔離（無外部依賴）、GitHub Actions CI 整合
- `tests/e2e/playwright.config.ts` + `.github/workflows/playwright.yml`

### CI/CD 改進

- tenant-api Go 測試納入 CI pipeline（2,115 行測試程式碼）
- Release 流程強化：`make pre-tag` 閘門、`bump_docs.py` 新增 tenant-api 版號線

### 數字

| 項目 | v2.4.0 | v2.5.0 | 變化 |
|------|--------|--------|------|
| JSX 互動工具 | 34 | 38 | +4 |
| ADRs | 9 | 10 | +1（ADR-010） |
| Playwright E2E specs | 0 | 5（38 test cases） | NEW |
| API 端點 | ~10 | ~16 | +6（groups + views） |

---

## [v2.4.0] — 防守深化 × 體質精簡 × 租戶管理 API (2026-04-05)

v2.4.0 的核心是「從能用到好管」：將 v2.3.0 release 暴露的手動痛點全面自動化（Phase A），對膨脹的核心檔案進行結構性重構（Phase B/B.5），引入 Tenant Management API 作為管理平面（Phase C），並重整 Playbook 體系（Phase D）。

### Phase A — 防守工具補強

將 v2.3.0 release 過程中手動發現的 6 類問題轉化為 pre-commit hook，auto hooks 從 13 個增至 19 個。

- **`check_build_completeness.py`**：`build.sh` ↔ `COMMAND_MAP` 雙向同步檢查，防止 Docker image 中工具遺漏
- **`check_bilingual_structure.py`**：ZH/EN 文件 heading hierarchy 骨架比對 + README 雙語導航對稱性
- **`check_jsx_i18n.py`**：`TOOL_META` ↔ `CUSTOM_FLOW_MAP` key set 一致性、`window.__t` 雙參數驗證
- **`check_makefile_targets.py`**：每個 `dx/generate_*.py` 和 `dx/sync_*.py` 工具被至少一個 Makefile target 引用
- **`check_metric_dictionary.py`**：`metric-dictionary.yaml` 與 Rule Pack YAML 交叉驗證，偵測 stale/undocumented entries
- **`check_cli_coverage.py` hook 化**：從測試升級為 pre-commit auto hook，cheat-sheet ↔ cli-reference ↔ COMMAND_MAP 三向一致
- **`_lint_helpers.py`**：抽取 `parse_command_map()`、`parse_build_sh_tools()`、`BUILD_EXEMPT` 等共用邏輯，消除 ~80 行重複

### Phase B — Go config.go 分拆 + 程式碼體質改善

- **config.go 拆分**（2,093 行 → 4 檔案）：`config_types.go`（268 行，型別定義）+ `config_parse.go`（277 行，YAML 解析）+ `config_resolve.go`（750 行，ResolveAt + 驗證）+ `config.go`（823 行，ConfigManager + 公開 API）
- 拆分為純結構移動，public API 語意不變，benchmark 差異 -0.3% ~ -5.0%（±5% 以內）
- **config_test.go table-driven 重構**：4,236 → 3,929 行（-7.2%），38 個重複 test function 收斂為 8 個 table-driven test，test function 總數 145 → 115
- Go 全部 145 測試通過，Python 3,657 passed / 44 skipped / 0 failed

### Phase B.5 — 文件與測試瘦身

Phase B 做到了「結構整理」，B.5 補做「內容精簡」。

- **合併 `context-diagram.md` → `architecture-and-design.md`**：~70% 重疊內容消除，淨刪 ~1,165 行，docs/ 檔案數 115 → 113
- **`incremental-migration-playbook.md` 瘦身**：1,165 行 → 575 行（-50.6%），冗長 JSON 範例改為摘要，手動 kubectl 序列改為 `da-tools` 命令
- **三態說明集中化**：`tenant-lifecycle.md` 的 60 行重複三態解釋改為 hyperlink + 3 行速查
- **版號全域修正**：44 處過時版號更新 + 文件計數修正
- 文件總計：docs/ -2,362 行（-6.4%），-2 個檔案

### Phase C — Tenant Management API（ADR-009）

新增 `components/tenant-api/` Go 元件，為 da-portal 加入 Backend API。

**架構決策（ADR-009）**
- API 語言選 Go：與 threshold-exporter 共用 `pkg/config` 解析邏輯，避免 Go↔Python 雙端維護
- 認證用 oauth2-proxy sidecar：API server 零 auth 程式碼，讀 `X-Forwarded-Email` / `X-Forwarded-Groups` header
- 寫回用 commit-on-write：UI 操作 → API → 修改 YAML → git commit（操作者名義），保留完整 audit trail
- RBAC 用 `_rbac.yaml` + `atomic.Value` 熱更新：lock-free 讀取，與 threshold-exporter reload 模式一致
- 不引入資料庫——Git repo 就是 database

**`pkg/config/` 抽取**
- 將 threshold-exporter 的型別與解析邏輯抽入 `components/threshold-exporter/app/pkg/config/`（`types.go` + `parse.go` + `resolve.go`）
- tenant-api 透過 `go.mod replace` directive 直接 import 共用型別

**API 端點**
- `GET /api/v1/tenants` — 租戶列表（支援 group/env 篩選）
- `GET/PUT /api/v1/tenants/{id}` — 單一租戶 CRUD
- `POST /api/v1/tenants/{id}/validate` — 乾跑驗證（不寫入）
- `POST /api/v1/tenants/batch` — 批量操作（`sync.Mutex` 同步，response 預留 `task_id`）
- `GET /api/v1/tenants/{id}/diff` — 預覽變更差異
- Health check / readiness probe / Prometheus metrics

**Portal 降級安全**：API 不可用時，tenant-manager.jsx 自動降級為 platform-data.json 唯讀模式。

**交付物**：Go binary + Docker image（distroless base）+ Helm chart + K8s manifests + 五線版號新增 `tenant-api/v*`

### Phase D — Playbook 重整 + 文件治理

- Playbook 結構化：testing-playbook 五段分層、benchmark-playbook 加入決策樹、windows-mcp-playbook 32 個 pitfall 分類索引
- `bump_docs.py` 自動計數功能：掃描並更新散落各處的工具數量、Rule Pack 數量等
- doc-map.md 自動生成預設包含 ADR

### 數字

| 項目 | v2.3.0 | v2.4.0 | 變化 |
|------|--------|--------|------|
| Pre-commit hooks | 13 auto + 7 manual | 19 auto + 9 manual | +6 auto, +2 manual |
| Go config.go | 2,093 行 × 1 檔 | 4 檔（268 + 277 + 750 + 823） | 結構拆分 |
| config_test.go | 4,236 行 / 145 函式 | 3,929 行 / 115 函式 | -7.2% / table-driven |
| docs/ 行數 | 37,059 | 34,697 | -2,362（-6.4%） |
| Components | 3 | 4（+ tenant-api） | +1 |
| ADRs | 8 | 9（+ ADR-009） | +1 |
| JSX 互動工具 | 29 | 34 | +5 |
| 版號線 | 4 | 5（+ tenant-api/v*） | +1 |
| Python 工具 | 84 | 91 | +7 |

---

## [v2.3.0] — Operator-Native × Management UI × Platform Maturity (2026-04-04)

v2.3.0 聚焦四大主題：Operator-Native 整合、Multi-Instance Management UI、Portal & Doc 成熟度、品質閘門升級。

### Phase .a — Portal & DX Foundation

**Self-Service Portal 模組化**
- `self-service-portal.jsx`（1,376 行）→ 5 個模組：`portal-shared.jsx`（共用常數/函式/元件）+ `YamlValidatorTab.jsx` + `AlertPreviewTab.jsx` + `RoutingTraceTab.jsx` + coordinator
- 新增 `dependencies` frontmatter 機制：jsx-loader.html 支援 YAML frontmatter 中宣告依賴，依序載入 → `loadDependency()` / `loadDependencies()` / `transformImports()`
- `window.__portalShared` 模式：共用模組透過全域變數註冊，tab 模組解構取用

**Template Gallery 外部化**
- 24 個模板 → `docs/assets/template-data.json`（雙語 `{zh, en}` 物件格式 + `category` 欄位）
- `template-gallery.jsx` 改為 `useEffect` fetch 載入，新增 loading/error 狀態
- 檔案大小：806 → 293 行（-64%）

**Portal Hub 五層重組**
- 29 個工具卡片從 2 區（Interactive / Advanced）→ 5 層級：Start Here、Day-to-Day、Explore & Learn、Simulate & Analyze、Platform Operations
- 新增 Quick Access 面板（5 個常用工具快捷連結）
- 每層級附色彩標籤（Onboarding / Core Workflow / Reference / What-If / Engineer）
- Role filter 同時作用於 Quick Access chips
- Tour 步驟更新、Footer 版號同步

**文件模板系統**
- 新增 `docs/internal/doc-template.md`：定義文件標準結構（frontmatter + 必要 section + Related Resources）
- 新增 `scripts/tools/lint/check_doc_template.py`：frontmatter 完整性 + Related Resources 存在性 + 版號一致性

**`_lib_python.py` 模組拆分**
- `_lib_python.py` → 4 個子模組：`_lib_constants.py`（守護值/常數）+ `_lib_io.py`（檔案 I/O）+ `_lib_validation.py`（驗證邏輯）+ `_lib_prometheus.py`（HTTP/Prometheus 查詢）
- 原檔保留為 re-export facade（向後相容，53 行）

**SAST Rule 7**
- 新增 `TestStderrRouting`：AST 掃描 `print("ERROR..."` / `print("Error..."` 確保附帶 `file=sys.stderr`
- 支援 literal string 和 f-string 兩種格式偵測

---

### Phase .b — Operator-Native + Federation

**ADR-008: Operator-Native Integration Path**
- 雙路整合架構決策：既有 ConfigMap 路徑保留，新增 Operator-Native 模式作為 BYO 方案
- 工具鏈適配而非平台重寫原則——threshold-exporter Go 核心語意不變
- 新增 `detectConfigSource()` 函式：逐級檢測 operator env var → git-sync `.git-revision` 文件 → configmap（預設）

**Prometheus Operator 整合指南**
- 新增 `docs/prometheus-operator-integration.md`（雙語 zh + en）：架構圖、CRD 對應、3 個部署場景（all-in-one / mixed / operator-only）
- BYO 文件清理：移除 Prometheus Operator appendices，改為重定向至新指南
- ServiceMonitor / PrometheusRule / AlertmanagerConfig CRD 映射表

**da-tools Operator 工具**
- **`da-tools operator-generate`** — 從 Rule Packs + Tenant 配置產生 PrometheusRule / AlertmanagerConfig / ServiceMonitor CRD YAML
  - 支援 `--namespace` / `--labels` / `--annotations` 自訂，`--output-format yaml | json`
  - 整合於 da-tools entrypoint + build.sh 打包
- **`da-tools operator-check`** — CRD 驗證工具：PrometheusRule 語法 + AlertmanagerConfig 路由合法性 + ServiceMonitor label 一致性
  - 支援 `--kubeconfig` / `--context` 直連 K8s 驗證，亦支援離線 YAML 驗證
  - Registered in CI lint pre-commit hooks

**Config Info Metric（四層感知）**
- 新增 `threshold_exporter_config_info{config_source, git_commit}` info metric
- 三種模式 + 自動偵測：
  - `configmap`（預設）：從 ConfigMap mount path 讀取 config version
  - `git-sync`：讀取 `.git-revision` 共享 volume 文件，提供 git commit SHA
  - `operator`：讀取 env var `CONFIG_SOURCE=operator` + `GIT_COMMIT=<sha>`
- `detectConfigSource()` 呼叫於 reload 時，確保 metric 實時反映部署形態

**Federation Scenario B（邊緣-中央分裂）**
- **`da-tools rule-pack-split`** — Rule Pack 聯邦分裂工具：
  - Part 1（正規化層）：邊緣側 metric value 驗證、單位轉換、異常值濾除 → 產生 Prometheus RecordingRules
  - Parts 2+3（閾值 + 警報層）：中央側聚合、cross-edge 關聯、全域告警決策 → 產生 Alerting Rules
  - 支援 `--operator` CRD 輸出 + `--gitops` 模式（目錄結構）
  - 關鍵特性：無狀態 split（idempotent）、邊緣 auto-healing（快照回滾）
- **`federation-integration.md` §8** — Scenario B 完整文件：三階段部署（邊緣佈建 → 中央策略 → 端對端驗證）、MTTR 優化、成本模型

**Go 單元測試（+12 tests，覆蓋率 87% → 94%）**
- WatchLoop 整合測試：無檔案變動 / 新增檔案 / 更新現有檔案
- `resolveConfigPath()` 三情案例：configmap flag / git-sync flag / 未設定（預設 configmap）
- `detectConfigSource()` 四情案例：configmap（預設）/ git-sync / operator / precedence（operator > git-sync > configmap）
- Config Info metric 收集器三情案例：各模式 value 驗證 + label 正確性
- Fail-Safe Reload E2E：config 不可讀時 fallback 邏輯

---

### Phase .c — Management UI + Intelligence

**Tenant Manager Data Foundation**
- 新增 `scripts/tools/dx/generate_tenant_metadata.py`：從 `conf.d/` 目錄結構推斷租戶 metadata
  - Rule Pack 推斷：根據 YAML 中 metric prefix 比對 Rule Pack 定義
  - 運營模式推斷：`_silent_mode` / `_state_maintenance` 標誌偵測
  - 路由通道推斷：`_routing` 配置解析
- 擴展 `scripts/tools/dx/generate_platform_data.py`：產出的 `platform-data.json` 新增 `tenant_groups` + `tenant_metadata` 結構
- Tenant metadata 版本化：支援 `--output-dir` 自訂輸出路徑，方便 GitOps 集成

**Tenant Manager UI 元件**
- 新增 `docs/interactive/tools/tenant-manager.jsx`（~650 行）：
  - 響應式卡片牆佈局，環境/層級徽章（dev/staging/prod + app/infra/platform）
  - 運營模式指示器：Normal / Silent / Maintenance 視覺標記 + expires 倒數
  - 批量操作：批次維護/靜默模式 YAML 產生器，支援日期範圍選擇
  - 篩選+搜尋：按環境/層級/模式多維度過濾，模糊搜尋租戶名
- 加入 `tool-registry.yaml` + Portal Hub Tier 1 (Day-to-Day 層級)

**閾值推薦 × Portal 智慧**
- 新增 `docs/assets/recommendation-data.json`：15 個核心指標的 P50/P95/P99 預計算資料
  - 資料來源：歷史基線 + 業界最佳實踐
  - 格式：`{metric_name: {p50, p95, p99, source, last_updated}}`
- 擴展 `docs/interactive/tools/AlertPreviewTab.jsx`：
  - Progress bar 上疊加 recommended value marker 視覺指示
  - Confidence badge（high/medium/low）顯示推薦可信度
  - 新增 "Apply Recommended Values" 按鈕，一鍵生成更新 YAML

**OPA/Rego 策略整合**
- 新增 `scripts/tools/ops/policy_opa_bridge.py`（~450 行）：tenant YAML → OPA input JSON 轉換 + 雙模式評估
  - 轉換函式：YAML 欄位 → OPA JSON 輸入格式映射（支援 nested policies）
  - 評估模式：REST API 模式（連接遠端 OPA 伺服器）+ 本地 opa binary 模式
  - 違規輸出格式轉換：OPA violations → da-tools 標準格式（location + description）
- `scripts/policies/examples/` 新增三個 Rego 範例策略：
  - `routing-compliance.rego`：路由規則命名 / receiver type / group_wait 範圍 validation
  - `threshold-bounds.rego`：閾值範圍檢查 / 關鍵指標預留冗餘
  - `naming-convention.rego`：租戶/告警 ID 命名規範 + Prefix 合法性
- 登記為 `da-tools opa-evaluate` 子命令 + CI lint 整合

**Portal i18n Lint 工具**
- 新增 `scripts/tools/lint/check_portal_i18n.py`（~250 行）：掃描 JSX 檔案尋找硬編碼字串
  - AST 解析：偵測 string literal 未用 `window.__t()` 包裝的情況
  - 支援 `--fix-mode`：自動生成修復建議（帶位置資訊）
  - 排除清單：URL / 特殊字元序列 / i18n 函式呼叫內部字串
- 加入 pre-commit manual-stage hooks 為 `check-portal-i18n`

---

### Phase .d — Quality Gate + CI Maturity

**GitHub Actions CI Matrix**
- 新增 `.github/workflows/ci.yml`：Python 3.10/3.13 × Go 1.22/1.26 矩陣（4 × 2 = 8 組合）
- 4 個主 jobs：lint（文件+工具格式）、python-tests（pytest + coverage）、go-tests（threshold-exporter）、lint-docs（SAST + doc 品質）
- pip/Go module 緩存策略、coverage artifacts 產生、失敗時自動 debug log 產出

**Coverage Gate 強制**
- `pyproject.toml` 新增 `fail_under = 85`，CI 強制 `--cov-fail-under=85` 執行
- README.md 新增 CI badge 與 coverage badge（green ≥85%、yellow 80–85%、red <80%）
- Python 工具預期整體覆蓋率 ≥85%

**Python 型別系統加強**
- `_lib_constants.py`、`_lib_io.py`、`_lib_validation.py`、`_lib_prometheus.py` 加入完整型別提示
- 新增 `mypy.ini`：strict mode for all `_lib_*` modules、relaxed mode for test files
- CI lint job 新增 `mypy scripts/tools/_lib_*.py --config-file=scripts/tools/mypy.ini` 步驟

**Integration + Snapshot 測試**
- `tests/test_tool_exit_codes.py`（parametrized）：全部 84+ 工具的 `--help` + invalid args exit code 合約測試
- `tests/test_pipeline_integration.py`：scaffold → validate → routes 完整 pipeline 端對端測試
- `tests/test_snapshot.py`：help output stability snapshot tests，支援 `--snapshot-update` CI 模式

**Pre-commit Hook 驗證確認**
- 確認 13 個 auto-run hooks + 7 個 manual-stage hooks 全部運作，Phase .a–.c 新增項目完全涵蓋
- `make pre-commit-audit` 新增 make 目標印出 hook 清單與觸發規則

---

## [v2.2.0] — 採用管線 + UX 升級 + 運維工具 (2026-03-17)

v2.2.0 聚焦三大主題：降低採用門檻的 Adoption Pipeline、Portal 互動體驗全面升級、配置運維新工具。新增 2 個 CLI 工具、3 個互動工具、Portal 三大 Tab 重構、24 個 Template Gallery 模板、5-tenant 展演腳本與 Hands-on Lab。

### 採用管線（Phase A — Adoption Pipeline）

- **`da-tools init`** — 專案骨架一鍵產生：CI/CD pipeline（GitHub Actions / GitLab CI）、`conf.d/` 目錄（含 `_defaults.yaml` + tenant YAML）、Kustomize overlays、`.pre-commit-config.da.yaml`，支援 `--non-interactive` 自動模式
- **GitOps CI/CD 整合指南** (`docs/scenarios/gitops-ci-integration.md`) — 三階段管線（Validate → Generate → Apply）、ArgoCD / Flux 整合、PR Comment Bot 工作流
- **Kustomize Overlays** — `configMapGenerator` 模式產生 threshold-config ConfigMap

### UX 升級（Phase B — Portal & Templates）

**Self-Service Portal 重構（3 Tab）**
- **Tab 1 (YAML Validation)**: Rule Pack 多選 → metric autocomplete → 動態 sample YAML 產生 → 即時驗證（含 pack-aware metric key 交叉檢查）
- **Tab 2 (Alert Preview)**: Pack-grouped 滑桿、視覺化閾值條、disabled/no-threshold 狀態顯示、severity dedup 說明
- **Tab 3 (Routing Trace)**: Metric+severity 輸入 → Alert origin → Inhibit check → 四層合併 → Domain Policy check → 通知派送 → NOC 副本

**Template Gallery 擴充（6 → 24 模板）**
- 7 場景模板：ecommerce、iot-pipeline、saas-backend、analytics、enterprise-db、event-driven、search-platform
- 13 Quick Start 模板：每個可選 Rule Pack 各一
- 4 特殊模板：maintenance、routing-profile、finance-compliance、minimal
- View mode 切換（All / Scenarios / Quick Start）+ Pack filter chips + Coverage summary

**新增互動工具**
- **CI/CD Setup Wizard** (`cicd-setup-wizard.jsx`) — 5 步精靈產生 `da-tools init` 命令：CI Platform → Deploy Mode → Rule Packs → Tenants → Review & Generate（第 27 個 JSX 工具）
- **Notification Template Previewer** (`notification-previewer.jsx`) — 6 種 receiver 通知預覽（Slack / Email / PagerDuty / Webhook / Teams / Rocket.Chat）+ Dual-Perspective annotation 展示 + Severity Dedup 說明（第 28 個）
- **Platform Health Dashboard** (`platform-health.jsx`) — 平台健康儀表板：元件狀態、租戶概覽、Rule Pack 使用分佈、Reload 事件時間線（第 29 個）

**展演與教學**
- **Demo Showcase** (`scripts/demo-showcase.sh`) — 5-tenant 完整展演腳本（prod-mariadb / prod-redis / prod-kafka / staging-pg / prod-oracle），7 步驟自動執行，支援 `--quick` 模式
- **Hands-on Lab** (`docs/scenarios/hands-on-lab.md`) — 30–45 分鐘 Docker-based 實戰教程，8 個練習覆蓋 init → validate → routes → routing trace → blast radius → three-state → domain policy

### 運維工具（Phase C — Operations）

- **`da-tools config-history`** — 配置快照與歷史追蹤：`snapshot` / `log` / `show` / `diff` 子命令，`.da-history/` 存儲，SHA-256 變更偵測，git-independent 輕量級版本控制

### 漸進式遷移 Playbook

- **`docs/scenarios/incremental-migration-playbook.md`** — 四階段雙軌並行遷移法（Strangler Fig Pattern）：Phase 0 Audit（`onboard` + `blind-spot`）→ Phase 1 Pilot（單一 domain 影子部署）→ Phase 2 Dual-Run（`shadow-verify` 品質比對）→ Phase 3 Cutover（逐 domain 切換）→ Phase 4 Cleanup。每步有 CLI 指令、預期輸出、回退方式
- **`architecture-and-design.md` §2.13** — 新增效能架構說明：Pre-computed Recording Rule vs Runtime Aggregation 的 PromQL 對比，解釋為什麼 tenant 增加不會導致 Prometheus CPU/Memory 暴增

### GitOps Native Mode

- **`da-tools init --config-source git`** — 產生 git-sync sidecar Kustomize overlay，threshold-exporter 直接從 Git 倉庫讀取配置，省去 ConfigMap 中間層。支援 SSH / HTTPS 認證、自訂分支與路徑。git-sync sidecar 寫入 emptyDir shared volume，threshold-exporter 的既有 Directory Scanner + SHA-256 hot-reload 機制無縫復用
- **`da-tools gitops-check`** — GitOps Native Mode 就緒度驗證工具，三個子命令：`repo`（Git 倉庫可達性 + 分支驗證）、`local`（本地 conf.d/ 結構驗證）、`sidecar`（K8s git-sync 部署狀態檢查），支援 `--json` 和 `--ci` 模式
- **Container Image Security Hardening** — 三層防護：base pin + build-time upgrade + attack surface reduction
  - threshold-exporter：`alpine` → `distroless/static-debian12:nonroot`（零 CVE，無 shell/apk/openssl）
  - da-tools：`python:3.13-alpine` → `python:3.13.3-alpine3.22` multi-stage build（修復 CVE-2025-48174, CVE-2025-15467）
  - da-portal：`nginx:1.28-alpine` → `nginx:1.28.0-alpine3.22` + `apk del libavif gd libxml2`（移除未使用 library，消除掃描器 false positive）

### 數字

| 項目 | v2.1.0 | v2.2.0 | 變化 |
|------|--------|--------|------|
| Python 工具 | 73 | 77 | +4 |
| da-tools CLI 命令 | 27 | 36 | +9 |
| JSX 互動工具 | 26 (+1 wizard) | 29 | +3 |
| Template Gallery 模板 | 6 | 24 | +18 |
| 場景文件 | 6 | 9 | +3 |
| Makefile targets | — | +1 (`demo-showcase`) | NEW |

---

## [v2.1.0] — 運維自助 + 告警智能化 + 性能優化 + 跨域路由 (2026-03-16)

v2.1.0 自 v2.0.0 起的全量升級。涵蓋 Go Exporter 增量熱載入、告警關聯分析、跨域路由架構 (ADR-006/007)、生態整合 (Backstage Plugin)、5 個新 CLI 工具、3 個互動工具、測試 +75%（1,759 → 3,070）、文件治理與正確性全面校正。

### Go Exporter 核心

**Incremental Hot-Reload (§5.6)**
- per-file SHA-256 index + parsed config cache，WatchLoop 增量重載路徑
- `ConfigManager` 新增 `fileHashes` / `fileConfigs` / `fileMtimes` 欄位
- `scanDirFileHashes()` — mtime guard + 輕量 hash check（mtime 未變直接跳過 I/O）
- `IncrementalLoad()` — 比對 per-file hash → 只重新解析 changed/added files → `mergePartialConfigs()`
- `fullDirLoad()` — 完整載入並初始化 cache（首次載入或 fallback）
- `applyBoundaryRules()` — 提取為獨立函式供共用
- **效能優化**：logConfigStats 取代 Resolve()、mtime guard、incremental merge（tenant 檔變動直接 patch）、byte cache（scan 快取復用，免除重複 I/O）
- 15 個 Go tests + 5 個 benchmarks（含 NoChange / OneFileChanged / ScanHashes / MergePartials）

**程式碼品質**
- 4 處 error print 修正為 `stderr` 輸出
- `parsePromDuration` / `isDisabled` / `clampDuration` 新增單元測試
- Go test 增加 config_test.go（801 行）+ config_bench_test.go（268 行）+ main_test.go（97 行）

### 跨域路由架構（ADR-006 + ADR-007）

**ADR-006: Tenant Mapping Topologies (1:1, N:1, 1:N)**
- 資料面映射方案：Prometheus Recording Rules 實現 1:N 映射（exporter 零修改）
- `generate_tenant_mapping_rules.py` — 讀取 `_instance_mapping.yaml`，產出 Recording Rules（36 tests）
- `scaffold_tenant.py` 新增 `--topology=1:N`、`--mapping-instance`、`--mapping-filter` 參數（9 tests）
- 範例設定檔 `_instance_mapping.yaml`

**ADR-007: Cross-Domain Routing Profiles**
- 四層合併管線：`_routing_defaults` → `routing_profiles[ref]` → tenant `_routing` → `_routing_enforced`
- `generate_alertmanager_routes.py` 擴展：profile 解析 + `check_domain_policies()` 驗證（21 tests）
- `scaffold_tenant.py` 新增 `--routing-profile` 參數
- 重構 `_parse_config_files()` → `_parse_platform_config()` + `_parse_tenant_overrides()` 子函式
- 範例設定檔 `_routing_profiles.yaml`、`_domain_policy.yaml`

**ADR-007 工具生態**
- `explain_route.py` — 路由合併管線除錯器：四層展開、`--show-profile-expansion`、`--json`、da-tools CLI 整合（25 tests）
- `check_routing_profiles.py` — CI lint 工具：未知 profile ref、孤立 profile、格式錯誤 constraints、`--strict` 模式（28 tests + pre-commit hook）

### 新增 CLI 工具

- **`da-tools test-notification`** — 6 種 receiver 連通性測試（webhook/slack/email/teams/pagerduty/rocketchat），Dry-run / CI gate / per-tenant 批次。57 tests，97% 覆蓋率
- **`da-tools threshold-recommend`** — 基於歷史 P50/P95/P99 的閾值推薦引擎，純 Python 統計，信心等級分級。54 tests，96% 覆蓋率
- **`da-tools alert-correlate`** — 告警關聯分析：時間窗口聚類 + 關聯分數 + 根因推斷，支援線上/離線模式。95% 覆蓋率
- **`da-tools drift-detect`** — 跨叢集配置漂移偵測：SHA-256 manifest 比對，pairwise 多目錄分析 + 修復建議。99% 覆蓋率
- **`da-tools explain-route`** — 路由合併管線除錯器（ADR-007），25 tests

### 生態整合

- **Backstage Plugin**：`components/backstage-plugin/` TypeScript/React plugin
  - `DynamicAlertingPage` + `DynamicAlertingEntityContent`
  - `PrometheusClient` API 層：via Backstage proxy 查詢 threshold / silent_mode / ALERTS
  - Entity 整合：`dynamic-alerting.io/tenant` annotation → 自動對應租戶

### 互動工具

- **Multi-Tenant Comparison** (`multi-tenant-comparison.jsx`)：Heatmap 色彩矩陣 + Outlier detection + Divergence Ranking（第 25 個 JSX 工具）
- **Alert Noise Analyzer** (`alert-noise-analyzer.jsx`)：MTTR 計算、震盪偵測、去重空間分析、Top noisy alerts（第 26 個）
- **ROI Calculator** (`roi-calculator.jsx`)：Rule 維護 / Alert Storm / Time-to-Market 三模型成本分析（第 27 個）

### DX Tooling

- **`check_frontmatter_versions.py`** — frontmatter version 全域掃描 + `--fix` 自動修復（29 tests）
- **`coverage_gap_analysis.py`** — per-file 覆蓋率排行報表（22 tests）
- **`check_bilingual_content.py`** — 雙語內容 CJK 比例 lint
- **`check_doc_links.py`** — 跨語言對應檔案驗證
- **`validate_all.py`** 增強：`--notify`（桌面通知）、`--diff-report`（CI 失敗自動 diff）
- **`generate_rule_pack_stats.py --format summary`** — Badge 風格單行輸出
- **Snapshot tests v2** — alert_correlate、drift_detect、bilingual_content 快照測試

### 安全加固

- SAST 規則擴充：6 rules 自動掃描（encoding + shell + chmod + yaml.safe_load + credentials + dangerous functions），189 patterns
- NetworkPolicy 精細化、container security context 強化
- 憑證掃描 + `.env` 防護 + `os.chmod 0o600` 補齊
- **CVE 緩解**：CVE-2025-15467 (openssl CVSS 9.8 pre-auth RCE) + CVE-2025-48174 (libavif buffer overflow)
  - 所有 Dockerfile 加入 `apk --no-cache upgrade` 拉取安全修補
  - `da-tools` base image pin 從 `python:3.13-alpine` → `python:3.13.2-alpine3.21`
- **CI Image Scanning**：release workflow 三個 image 均加入 Trivy 掃描（CRITICAL + HIGH 阻斷）

### 品質閘門

- Pre-commit hooks：12 → **13** 個 auto-run（新增 `routing-profiles-check`）
- `build.sh` 修補：新增遺漏的 `alert_correlate`、`notification_tester`、`threshold_recommend` 打包

### 測試覆蓋率

Python 測試總數從 v2.0.0 的 1,759 提升至 **3,070**（+75%）。v2.1.0 新增工具均達 95%+ 覆蓋率，5 個既有工具從 41–74% 提升至 63–99%。Coverage gate 維持 `fail_under=64`，實際整體覆蓋率高於此基線。

### 數字

| 項目 | v2.0.0 | v2.1.0 | 變化 |
|------|--------|--------|------|
| Python 工具 | 62 | 73 | +11 |
| da-tools CLI 命令 | 23 | 27 | +4 |
| JSX 互動工具 | 24 | 26 (+1 wizard) | +3 |
| ADRs | 5 | 7（006/007 Accepted） | +2 |
| Python 測試 | 1,759 | 3,070 | +1,311 |
| Pre-commit hooks | 12 + 5 manual | 13 + 5 manual | +1 |
| Go tests (new files) | — | +3 files (1,166 lines) | NEW |

### Benchmark — Incremental Hot-Reload（Go, `-count=3` median）

| Benchmark | ns/op | B/op | allocs/op |
|-----------|------:|-----:|----------:|
| IncrementalLoad_NoChange_10 | 165,700 | 34,272 | 176 |
| IncrementalLoad_NoChange_1000 | 1,528,000 | 2,027,264 | 13,085 |
| IncrementalLoad_OneFileChanged_10 | 220,600 | 73,280 | 241 |
| IncrementalLoad_OneFileChanged_1000 | 6,908,000 | 6,652,880 | 22,211 |
| ScanDirFileHashes_1000 | 1,206,000 | 1,985,200 | 13,012 |

### 文件治理與正確性

**Root README (zh/en) 增強**
- 開頭改為問題導向定位（規則膨脹 + 變更瓶頸），新增「適用場景」聲明與版本 badge
- 「關鍵設計決策」表新增 ADR 連結欄 + Sentinel 三態控制、四層路由合併兩行
- Quick Start 下方新增「生產部署」指引，文件導覽新增 Day-2 Operations 路徑

**ADR 生命週期更新**
- ADR-006/007：`📋 Proposed` → `✅ Accepted (v2.1.0)`，checklist 改為實作摘要 + 後續方向
- ADR-004：「現況與後續方向」替代舊 Roadmap 段落
- ADR-001/003：新增 v2.1.0 living-doc 狀態行
- ADR-002/004：新增「相關決策」交叉引用（ADR-005/006）

**architecture-and-design.md**
- 新增 §2.12 Routing Profiles 與 Domain Policies（四層合併管線 Mermaid 圖）
- 「本文涵蓋內容」補上三態模式、Dedup、路由系統
- 拆分文件導覽表移除過時 §N 前綴
- ADR-006 工具引用、Rule Pack 數量修正、雙語 annotation 章節翻譯

**benchmarks.md 重構**
- §8（Alertmanager Idle-State）合併至 §10（Under-Load）作為 baseline 比較表
- §13（pytest-benchmark）去重：移除與 §7 重複的 route generation 行
- 傳統方案估算加註推算基礎（per-rule ~0.3ms / ~60KB）
- 自引用修正、相關資源連結格式修正

**docs/README.md (zh/en) 去重**
- 移除與 root README 重複的「工具速查」22 行表 → 精簡為摘要 + 連結
- 移除重複的「快速命令」和「版本與維護」段落

**Component README 修正**
- threshold-exporter：斷裂 §11.1 引用 → 指向 `gitops-deployment.md`
- da-tools：版號表 `v2.0.0` → `v2.1.0`，移除過時措辭
- da-portal：`24 JSX tools` → `26`，image tag `v2.0.0` → `v2.1.0`
- backstage-plugin：移除不存在的 `(§5.13)` 引用

**交叉引用修正**
- ADR-006 (zh/en)：`§2.6` → `§2.3`（Tenant-Namespace 映射模式）
- ADR README (zh/en)：ADR-006/007 badge 更新為 ✅ Accepted

### 🐛 Bug Fixes

- 修復 `entrypoint.py` help text 遺漏 `validate-config` 命令
- 4 處 Python error output 修正為 stderr
- `da-tools build.sh` TOOL_FILES 補齊遺漏工具

---

## [v2.0.0] — Alert Intelligence + Full-Stack DX Overhaul (2026-03-15)

v2.0.0 正式版。自 v1.11.0 起的全量升級：76 個 commits、346 個檔案變更（+73,057 / -12,023）。涵蓋 Go Exporter 增強、Rule Pack 擴展、告警智能化、互動工具生態、文件全面重構、測試工程化、專案結構正規化。

> **版號說明**：v1.12.0 / v1.13.0 / v2.0.0-preview 系列皆為開發中版本（無 Git tag / GitHub Release），統一於 `v2.6.0` 正式釋出。

### 🔧 Go Exporter 增強

**Tenant Profiles（四層繼承）**
- Go schema 新增 `Profiles map[string]map[string]ScheduledValue` 欄位
- `applyProfiles()` fill-in pattern：Load 階段展開 profile 至 tenant overrides（僅填入未設定的 key）
- `_profiles.yaml` boundary enforcement：LoadDir 限制 profiles 只能從該檔載入
- `ValidateTenantKeys()` 擴展：`_profile` 引用不存在的 profile → WARN
- 繼承順序：Global Defaults → Profile → Tenant Override（tenant 永遠勝出）
- 13 個新 Go 測試案例

**Dual-Perspective Annotation**
- `platform_summary` annotation：Alert 同時攜帶 Platform 視角（NOC）和 Tenant 視角 summary
- 與 `_routing_enforced` 整合：NOC 收到 `platform_summary`，tenant 收到原始 `summary`

### 📦 Rule Pack 擴展（13 → 15）

- **JVM Rule Pack** (`rule-pack-jvm.yaml`)：GC pause rate、heap memory usage、thread pool — 7 alert rules（含 composite `JVMPerformanceDegraded`）
- **Nginx Rule Pack** (`rule-pack-nginx.yaml`)：active connections、request rate、connection backlog — 6 alert rules
- Projected Volume 13 → 15 ConfigMap sources，scaffold_tenant / metric-dictionary 同步更新

### 🚀 告警智能化（3 個新工具 + 1 個 Self-Service Portal）

**Alert Quality Scoring (`da-tools alert-quality`)**
- 4 項品質指標：Noise（震盪偵測）、Stale（閒置 14 天）、Resolution Latency（flapping 警告）、Suppression Ratio
- 三級評分（GOOD/WARN/BAD）+ per-tenant 加權分數（0–100）
- 輸出：text / `--json` / `--markdown`，CI gate：`--ci --min-score 60`
- 57 個測試，89.8% 覆蓋率

**Policy-as-Code (`da-tools evaluate-policy`)**
- 宣告式 DSL：10 種運算子（required / forbidden / gte / lte / matches / one_of ...）
- `when` 條件式、萬用字元目標（`*_cpu`）、dot-path 嵌套（`_routing.receiver.type`）
- Duration 比較、tenant 排除、error/warning 雙嚴重度
- CI gate：`--ci` 有 error 違規 exit 1
- 106 個測試，94.0% 覆蓋率

**Cardinality Forecasting (`da-tools cardinality-forecast`)**
- 純 Python 線性回歸（無 numpy）：趨勢分類（growing/stable/declining）+ 風險等級（critical/warning/safe）
- 觸頂天數預測 + 預計日期，可設基數上限（`--limit`）和預警天數（`--warn-days`）
- CI gate：`--ci` 有 critical 風險 exit 1
- 61 個測試，93.5% 覆蓋率

**Tenant Self-Service Portal (`self-service-portal.jsx`)**
- 三分頁 SPA：YAML 驗證（schema + routing guardrails）、告警預覽（滑桿模擬）、路由視覺化（樹狀圖）
- 瀏覽器端執行，零後端依賴，雙語支援（zh/en）

**Self-Hosted Portal (`da-portal` Docker image)**
- `ghcr.io/vencil/da-portal` — nginx:alpine 靜態 image，打包 24 JSX tools + Hub + Guided Flows + vendor JS
- 企業內網 / air-gapped 部署：`docker run -p 8080:80`，免 build step
- Volume mount 客製化：`platform-data.json`、`flows.json`、`nginx.conf`（含 Prometheus reverse proxy placeholder 解決 CORS）
- CI/CD：`portal/v*` tag 觸發 `release.yaml` 自動 build + push GHCR

### 🛠️ DX 自動化工具（+8 個新工具）

**Operations**
- **`shadow_verify.py`**：Shadow Monitoring 三階段驗證（preflight / runtime / convergence）
- **`byo_check.py`**：BYO Prometheus & Alertmanager 整合驗證（取代手動 curl + jq）
- **`grafana_import.py`**：Grafana Dashboard ConfigMap 匯入（sidecar 掛載 + verify + dry-run）
- **`federation_check.py`**：多叢集 Federation 整合驗證（edge / central / e2e 三模式）

**Scalable Configuration Governance**
- **`assemble_config_dir.py`**：Sharded GitOps 組裝工具 — 多來源 conf.d/ 合併、SHA-256 衝突偵測、assembly manifest
- **`da_assembler.py`**：ThresholdConfig CRD → YAML 輕量 controller（Watch / One-shot / 離線渲染 / Dry-run）
- **ThresholdConfig CRD**（`dynamicalerting.io/v1alpha1`）：namespace-scoped + RBAC + printer columns

**DX 工具迭代**
- `validate_all.py`：`--profile` + `--watch`（CSV timing trend）、`--smart`（git diff → affected-check 自動跳過）
- `bump_docs.py`：`--what-if`（全 238 rules 審計）
- `generate_cheat_sheet.py` / `generate_rule_pack_stats.py`：`--lang zh/en/all` 雙語
- `check_doc_freshness.py`：false-positive 修正 + `--fix`
- `check_translation.py`：cross-dir + lang fix
- `check_includes_sync.py`：`--fix`（自動建立缺失 .en.md stub）

### 🎯 互動工具生態（0 → 24 JSX tools）

**工具矩陣**：23 個位於 `docs/interactive/tools/` + 1 個 `docs/getting-started/wizard.jsx`
- Config：Playground、Lint、Diff、Schema Explorer、Template Gallery
- Rule Pack：Selector、Matrix、Detail、PromQL Tester
- 運維：Alert Simulator/Timeline、Health Dashboard、Capacity Planner、Threshold Calculator
- 學習：Architecture Quiz、Glossary、Dependency Graph、Runbook Viewer、Onboarding Checklist
- 展示：Platform Demo、Migration Simulator、CLI Playground、Self-Service Portal

**基礎設施**
- **tool-registry.yaml**（單一真相源）→ `sync_tool_registry.py`（`make sync-tools`）自動同步 Hub 卡片 + TOOL_META + JSX frontmatter
- **platform-data.json**（共用資料源）：從 Rule Pack YAML 萃取（15 packs, 139R + 99A），JSX 工具 fetch 共用
- **jsx-loader.html**：瀏覽器端 JSX transpiler + `TOOL_META`（related footer）+ `__PLATFORM_DATA` 預載 + Guided Flow 模式
- **tool-consistency-check**（pre-commit）：Registry ↔ Hub ↔ TOOL_META ↔ JSX ↔ MD 一致性驗證

**Guided Flows**
- `flows.json` 多步引導流程（onboarding / tenant-setup / alert-deep-dive），`?flow=onboarding` 啟動
- Cross-step data（`__FLOW_STATE` + sessionStorage）、progress persistence、completion tracking
- Conditional steps + checkpoint validation（`__checkFlowGate()` Next 按鈕閘門）
- Custom flow builder：`?flow=custom&tools=...` Hub 互動式 builder，24 工具全覆蓋
- Flow analytics：進度條、完成率、drop-off 步驟偵測

### 🌐 Bilingual Annotations (i18n)

- **Rule Pack 雙語 annotation**：`summary_zh` / `description_zh` / `platform_summary_zh` — 三個 Pilot Pack（MariaDB, PostgreSQL, Kubernetes）
- **Alertmanager template fallback**：Go `or` function 優先中文、自動 fallback 英文（所有 receiver 類型）
- **CLI i18n**：`detect_cli_lang()` 偵測 `DA_LANG`/`LANG` → argparse help 雙語切換（23 個 CLI 命令）
- **check_bilingual_annotations.py**：Rule Pack 雙語覆蓋率驗證（pre-commit manual stage）

### 📄 文件全面重構

**結構重組**
- architecture-and-design.md 拆分為 6 個專題文件（benchmarks / governance-security / troubleshooting / migration-engine / federation-integration / byo-prometheus-integration）
- 3 個角色入門指南（for-platform-engineers / for-domain-experts / for-tenants）zh/en
- 全面雙語化：33 → 46 對 `.en.md` 文件
- MkDocs Material 站點：CJK 搜尋、tags、i18n 切換、abbreviation tooltips
- Glossary（30+ 術語）+ 5 ADRs + JSON Schema（VS Code 自動補全）

**內容修訂**
- 根 README (zh/en) 重寫：角色導向痛點敘事（Platform / Tenant / Domain / Enterprise）
- architecture-and-design.en.md：補 §2.3 Tenant-Namespace Mapping、修 §3.1（15 packs + `prometheus-rules-*` 命名）、補 Bilingual Annotations
- Benchmarks 重寫：5 輪實測數據統一採集（idle + under-load + routing + alertmanager + reload）
- 6 份文件精簡（avg -23%）：移除過時內容、手動 curl 改為 da-tools CLI 引用
- Scenario CLI 修正：`tenant-lifecycle.md` (zh/en) 修正 4 個不存在的 CLI flags
- Tool-map 重生成：62 個工具完整覆蓋（之前僅 18 個）

**文件 CI 工具鏈（13 tools）**
- `validate_mermaid.py` / `check_doc_links.py` / `check_doc_freshness.py` / `doc_coverage.py`
- `add_frontmatter.py` / `doc_impact.py` / `check_translation.py` / `check_includes_sync.py`
- `sync_glossary_abbr.py` / `sync_schema.py` / `generate_cheat_sheet.py` / `inject_related_docs.py`
- `validate_all.py`：統一驗證入口

### 🔒 Security Audit & Hardening

- **程式碼安全**：ReDoS 防護（regex 長度限制）、URL 注入白名單、SSRF scheme 白名單（http/https only）、Prototype pollution 過濾（`__proto__`/`constructor`）、YAML 100KB 上限、`os.chmod` 補齊
- **文件安全加固**：HTTP→HTTPS 範例、webhook 驗證升為 error、`--web.enable-lifecycle` 安全註解、Grafana 密碼警告、新增「生產環境安全加固」章節

### 🏗️ 專案結構正規化

- **scripts/tools/ 三層子目錄化**：62 個工具分入 `ops/`（30）、`dx/`（18）、`lint/`（13）+ root（1 + 1 lib）
  * Docker flat layout 相容（dual sys.path + build.sh 自動 strip）
- **JSX 工具搬遷**：22 個工具 `docs/` → `docs/interactive/tools/`，registry/flows/loader/hub 路徑同步
- **測試歸位**：`test_assemble_config_dir.py`、`test_da_assembler.py`、`test_flows_e2e.py` 統一搬入 `tests/`
- **generate_tool_map.py 重寫**：自動掃描 ops/dx/lint/root 子目錄

### 🧪 測試工程化（14 輪系統化重構）

| 項目 | v1.11.0 | v2.0.0 | 變化 |
|------|---------|--------|------|
| 測試檔案 | 5 | 40 | +35 |
| 測試數量 | ~790 | 1,759 | +969 |
| Go 測試 | 97 | 110 | +13 |
| Coverage gate | 無 | 64%（`setup.cfg`） | NEW |
| Test markers | 無 | 5（slow/integration/benchmark/regression/snapshot） | NEW |
| Factories | 無 | 12（`factories.py` + `PipelineBuilder`） | NEW |

**關鍵里程碑**：
- Wave 5-6：pytest 遷移、SAST 掃描器（189 rules）、整合測試
- Wave 7-8：property-based tests（Hypothesis）、snapshot tests（18 JSON）、coverage gate
- Wave 9-10：factories 拆分、domain policy、deepdiff structured diff
- Wave 11-12：unittest→pytest batch migration、metric_dictionary fixture
- Wave 13：conftest re-export cleanup、duplicate removal、factory docstrings
- Wave 14-16：parametrize、scaffold snapshots、benchmark baseline、validate_all coverage
- Wave 17：coverage attack — baseline_discovery（31→55%）、backtest_threshold（32→70%）、batch_diagnose（49→71%）
- Wave 18：parametrize sweep — 合併重複測試方法

### 🛡️ 品質閘門

- **Pre-commit hooks**：0 → 12 個 auto-run + 5 個 manual-stage（schema / translation / flow E2E / jsx-babel / i18n coverage）
- **新增 hooks**：`tool-map-check`、`doc-map-check`、`rule-pack-stats-check`、`glossary-check`、`changelog-lint`、`version-consistency`、`includes-sync`、`platform-data-check`、`repo-name-check`、`tool-consistency-check`、`structure-check`、`doc-links-check`
- **Docker CI 修正**：build.sh 自動 strip sys.path hack + 觸發路徑 `**/*.py` + 3 個遺漏工具打包修正
- **Conventional Commits** + `generate_changelog.py` 自動化

### 📦 Dependency Upgrades

- **Prometheus**: v2.53.0 → v3.10.0（PromQL 相容性已驗證，15 個 Rule Pack 無影響）
- **Alertmanager**: v0.27.0 → v0.31.1
- **configmap-reload**: v0.14.0 → v0.15.0
- **Grafana**: 11.1.0 → 12.4.1
- **kube-state-metrics**: v2.10.0 → v2.18.0
- **Go**: 1.22 → 1.26.1（go.mod + Dockerfile + CI）
- **Frontend CDN**: React 18.2.0 → 18.3.1、Babel 7.23.9 → 7.26.4、Lucide 0.383.0 → 0.436.0

### 📊 Numbers

| 項目 | v1.11.0 | v2.0.0 | 變化 |
|------|---------|--------|------|
| Rule Packs | 13 | 15 | +2 |
| Python 工具 | ~20 | 62 | +42 |
| da-tools CLI 命令 | 20 | 23 | +3 |
| JSX 互動工具 | 0 | 24 | +24 |
| 文件（docs/ .md） | ~20 | 68 | +48 |
| 雙語文件對 | 0 | 46 | +46 |
| Python 測試 | ~790 | 1,759 | +969 |
| 測試檔案 | 5 | 40 | +35 |
| Pre-commit hooks | 0 | 12 + 5 manual | +17 |
| Docker images | 2 | 3 | +1 (da-portal) |

### 📈 Benchmark（v2.0.0，15 Rule Packs，Kind 叢集）

**Idle-State（2 tenant，237 rules，43 rule groups）：**

| 指標 | v1.11.0 (13 packs) | v2.0.0 (15 packs) | 變化 |
|------|-------|-------|------|
| Total Rules | 141 | 237 | +96 |
| Rule Groups | 27 | 43 | +16 |
| Eval Time / Cycle | 20.3ms | 23.2ms | +2.9ms |
| p50 per-group | 1.23ms | 0.39ms | 改善 |
| p99 per-group | 6.89ms | 4.89ms | 改善 |
| Prometheus CPU | 0.014 cores | 0.004 cores | — |
| Prometheus Memory | 142.7MB | 112.6MB | — |
| Exporter Heap (×2 HA) | 2.4MB | 2.2MB | — |
| Active Series | ~6,037 | 6,239 | +202 |

**Go Micro-Benchmark（Intel Core 7 240H，`-count=5` median）：**

| Benchmark | ns/op (median) | B/op | allocs/op |
|-----------|------:|-----:|----------:|
| Resolve_10Tenants_Scalar | 12,209 | 26,488 | 61 |
| Resolve_100Tenants_Scalar | 100,400 | 202,777 | 520 |
| Resolve_1000Tenants_Scalar | 1,951,206 | 3,848,574 | 5,039 |
| ResolveAt_10Tenants_Mixed | 34,048 | 40,052 | 271 |
| ResolveAt_100Tenants_Mixed | 405,797 | 462,636 | 2,622 |
| ResolveAt_1000Tenants_Mixed | 5,337,575 | 5,258,548 | 26,056 |
| ResolveAt_NightWindow_1000 | 5,404,213 | 5,223,925 | 25,056 |
| ResolveSilentModes_1000 | 86,700 | 186,086 | 10 |

**Route Generation Scaling（Python `generate_alertmanager_routes.py`）：**

| Tenants | Wall Time | Routes | Inhibit Rules |
|---------|-----------|--------|---------------|
| 2 | 181ms | 3 | 2 |
| 10 | 196ms | 8 | 10 |
| 50 | 248ms | 41 | 50 |
| 100 | 327ms | 80 | 100 |

---

> **歷史版本 (v0.1.0–v1.11.0)：** 詳見 [`CHANGELOG-archive.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CHANGELOG-archive.md)
