---
title: "CLAUDE.md — AI 開發上下文指引"
tags: [ai-agent, onboarding, internal]
audience: [ai-agent, maintainers]
version: v2.9.0
lang: zh
---

# CLAUDE.md — AI 開發上下文指引

## ⛔ Agent 起手式（已自動化 🛡️）

Session 起手式 codified 為 **PreToolUse hook** (v2.8.0；#824 改經 `run-hooks.sh` launcher 做直譯器功能性探測) — 第一次 `Bash`/`Write`/`Edit`/`MultiEdit` 自動跑 `scripts/session-guards/session-init.py`（關 VS Code Git + 寫 session marker + 刷 liveness heartbeat），後續 O(1) no-op。手動觸發 / telemetry / dev-container 啟動 / session 結束清理 → 觸發 `vibe-workflow` skill 或 `make dc-*` / `make session-cleanup`。

第二支 PreToolUse hook：`scripts/session-guards/preflight_bash.py`（audit-2026-04 §H1+H2）— 攔 `sed -i` + 掛載路徑（dev-rules #11，免 token 浪費 fix file hygiene）+ 攔 `_*.bat`/`_*.ps1`/`_*.cmd` 寫到 whitelist 之外（Trap #54 防再造輪子）。被擋時 stderr 直接告訴 Claude 該用什麼替代（Read+Edit / `win_git_escape.bat raw <args>`）。

### 設計原則：主路徑 / 逃生門

> **主路徑** Dev Container 做所有事（code/test/commit/push，優先 `make dc-*`）；**逃生門** FUSE 卡死用 Windows 原生 git（`make win-commit` / `scripts/ops/win_git_escape.bat`）。目標：不讓任何 session 因 FUSE 卡死。

## ⛔ 高頻地雷（always-on，已升 root；TRK-302）

被燒過 ≥2 次或高頻的規則，從 lazy-load feedback 升為 always-on（不靠 keyword 觸發）：

1. **回應語言** — user-facing prose 一律**繁體中文**（非日文 / 英文）
2. **Commit trailer block** — 所有 trailer 行（`Refs:` / `Self-Review-Pass-2:` / `Co-authored-by:`）須為**最底部單一連續段落、全 `Key: value` 格式**；夾空行或無冒號裸行會劈裂 block → git 丟棄上方行 → CI gate fail（燒過 #515/#522/#543）。多項目 / 純文件 commit 依 [`dev-rules.md` §P1](docs/internal/dev-rules.md) 改在 body prose 列 ID，**不寫 `Resolves` 裸行**
3. **Worktree edit path** — 在 git worktree 內編輯須 anchor worktree 路徑；main repo 同時 checked out，用 main-repo 路徑會悄悄落到 main（燒過 #562）
4. **`git add` 括號 glob** — bash `[01]` 只配 `0`/`1` 不配 `2`；任何括號 glob 後必跑 `git diff --cached --stat` 驗 staged set（燒過 #485 ~2h）
5. **commit / push 前先觸發 `vibe-dev-rules` skill** — pre-commit hook 不攔所有 Vibe gate（如 `make lint-docs-mkdocs`）；skip-and-recover 浪費 2+ push cycle

## Skill 體系

Vibe 專案內建 **七個本地 skills**（`.claude/skills/`），在對應情境自動觸發：

- **`vibe-workflow`** — session 起手式、7 個常見陷阱、標準開發工作流（session 開始或遇到 FUSE / docker / port-forward 類問題時自動觸發）
- **`vibe-dev-rules`** — 13 條開發規範 + Top 4 違反熱點（commit / push / refactor 前自動觸發）
- **`vibe-playbook-nav`** — 任務→Playbook 章節路由（涉及 K8s / docker / release / conf.d / benchmark / E2E 時自動觸發）
- **`vibe-subagent-review`** — IaC-aware 兩階段 review（code 走 spec→quality、IaC 走 blast-radius）+ 長時驗證 agent 可觀測性協議（Workflow-first / `dev/<scope>/PROGRESS.jsonl` ledger / 單 agent ~15 min 上限 / `make agent-progress`）（multi-file PR / `Agent` 跑完後、commit 前、或 spawn 長時 reviewer/verifier 前自動觸發；TRK-305）
- **`vibe-release`** — 六線版號 release 收尾 SOP（make pre-tag → CHANGELOG distill + project-face refresh → 六線 tag → gh release ×6；release 收尾 / phase e 時觸發；TRK-306，延伸 #474 Layer 3）
- **`vibe-brainstorm`** — 設計階段 Socratic ideation（MVP 範圍 / explicit trade-off / defer-with-trigger + 外部 adversarial review；新 ADR / component / epic 拆解 / RFC 時觸發；TRK-308）
- **`vibe-security-audit`** — 全 component 週期性深度安全稽核 harness（Recon→平行 Hunt→對抗式 Validate→Synthesize，跑在隔離 worktree 快照；借 Cloudflare `security-audit-skill` pattern wrap Vibe 攻擊面向，per-role 走 `.claude/agents/vibe-sec-*`；新信任邊界 GA 前 / incident 後 / 季度觸發，與 diff-scoped `/security-review` 互補、不進 CI）

環境層 skills（`docx` / `pptx` / `xlsx` / `pdf` / `engineering:*` / `data:*` / `design:*` / `marketing:*` 等）**Claude 自主判斷使用**，不需逐次徵詢：判斷符合即讀 SKILL.md 執行（使用前單行說明，如「跑 `engineering:debug` reproduce 步驟」）、多 skill 自主串接、發現該裝沒裝的用 `mcp__plugins__search_plugins` / `mcp__mcp-registry__search_mcp_registry` 主動找 + 建議。

### Skill 優先級宣告（衝突仲裁；TRK-301）

多 skill 同時匹配時，本地 `vibe-*` 優先於環境層 generic（僅「Vibe 已有專屬流程」範圍）：`vibe-workflow` > 環境層 session-bootstrap 指引、`vibe-dev-rules` > `engineering:code-review` 的 git/commit/branch/trailer 部分、`vibe-playbook-nav` > 跨 K8s/Helm/release/E2E generic。環境層 skill 仍負責其專業領域（`engineering:debug` reproduce、`data:*` 分析等），不在此範圍者照常自主使用。

## 專案概覽

**Multi-Tenant Dynamic Alerting 平台 (v2.9.0)** — Config-driven, SHA-256 hot-reload, Directory Scanner。完整架構速覽見 [architecture-and-design.md](docs/architecture-and-design.md)；版本歷程見 [CHANGELOG.md](CHANGELOG.md)。**v2.9.0 已發版**（2026-06-06，五線 tag GA — 租戶自助告警 Custom Alerts（ADR-024 能力 B）+ 租戶聯邦（ADR-020 outline→可部署）+ 寫入平面 single-writer 韌性（ADR-023）+ 平台日誌彙整；詳 CHANGELOG `## [v2.9.0]`）。前兩版 **v2.8.1**（2026-05-16，平台 tag only 的 DX / 內部工具 interim：secret-scan 四層防線 + Planning SSOT 自動化）、**v2.8.0**（2026-05-12，客戶導入管線 + 千租戶 Scale 驗證 + supply-chain provenance）。目前 **v2.10.0 開發中**（in-flight 工作見 CHANGELOG `## [Unreleased]`）。

## 架構速查

9 個核心設計概念（Severity Dedup / Sentinel Alert / Routing Guardrails / Schema Validation / Cardinality Guard / 三態 / Dual-Perspective / 四層路由 / Tenant API）見 [architecture-and-design.md §設計概念總覽](docs/architecture-and-design.md#設計概念總覽)。spoke 文件在 [`docs/design/`](docs/design/)。

**try-local 一鍵體驗**（#449 epic）：`cd try-local && cp .env.example .env && docker compose up -d`（不需 K8s）起整套 showcase stack，~1min 看到真實 critical 告警紅燈；Mode 0 核心雙星 `docker compose up da-portal tenant-api` 只起 live Tenant Manager（Save→真實 git commit）。tenant-api 本機用 `--dev-bypass-auth`（[ADR-022](docs/adr/022-dev-auth-bypass-four-layer-containment.md) 四層防線）。見 [`try-local/README.md`](try-local/README.md)。

**Secret-scan 四層防線**（v2.8.1 #445）：L0 GitHub native push-protection → L1 pre-commit hook `secrets-scan-staged` → L2 server-side `secret-scan.yml`（不可繞）→ L3 release-time image digest verification。incident response 走 [`secret-leak-remediation-sop.md`](docs/internal/secret-leak-remediation-sop.md)（ASSUME COMPROMISE / ROTATE FIRST）；規範見 [`dev-rules.md` §安全紀律](docs/internal/dev-rules.md)。

**Container/k8s IaC SAST 四層防線**（v2.9.0 #448，與上互補）：L1 Dockerfile（hadolint）／ L2 Helm template + L4 raw k8s manifest（kube-linter）／ L3 values+manifest secret-shape（Vibe wrapper，與 #445 trufflehog 高熵互補）。**hybrid policy**（open-source engine + Vibe wrapper 取代 DIY-only，僅 greenfield）；Critical → BLOCK（required status check）、High → 中央 EXEMPTIONS 列管。consolidated baseline + Severity→Action SSOT + branch-protection checklist 見 [`iac-lint-baseline.md`](docs/internal/iac-lint-baseline.md)。

## 開發規範（Top 4 熱點）

13 條完整規範見 [`docs/internal/dev-rules.md`](docs/internal/dev-rules.md)；完整 Top 4 說明 + 互動工具變更 SOP → 觸發 `vibe-dev-rules` skill。

1. **#12 Branch + PR** — ⛔ **禁止直推 main**。一律開 branch → PR → owner 同意後 merge。pre-push hook 攔截（`scripts/ops/protect_main_push.sh` + `scripts/ops/require_preflight_pass.sh`）
2. **#11 檔案衛生** — 禁止對掛載路徑用 `sed -i`（會截斷缺少 EOF 換行的檔案）。用 Read+Edit 或 pipe
3. **#4 Doc-as-Code** — 影響 API / schema / CLI / 計數的變更須同步 `CHANGELOG.md` + `CLAUDE.md` + `README.md`
4. **#2 Tenant-Agnostic** — Go / PromQL / fixture 禁止 hardcode tenant id（例如 `db-a`）

## 測試注入 Seam（v2.8.0 後標準）

`ConfigManager` 三個 test-only setter（`SetMetrics` / `SetLogger` / `startWatchLoopWithFakeClock`）取代了 v2.7.x 的 global-swap antipatterns。**寫新測試鐵則：用對 seam、禁止再引入 global swap**；完整對照表（freshMetrics / fake clock / 已移除的 withIsolatedMetrics 等）+ 加 `t.Parallel` 的 RISKY 決策樹見 [`test-map.md` §測試注入 Seam](docs/internal/test-map.md#測試注入-seam-v280-後標準)（`add_t_parallel.py` 的 RISKY tuple 同時當 lint tripwire）。

## 語言策略（SSOT Language）

**Policy locked（v2.8.0 S#101 closure）**：**中文為主 SSOT + 英文為輔**（`foo.md` ZH / `foo.en.md` EN）。**不執行 ZH→EN 遷移**；既有客戶與貢獻者社群均為中文母語，原 v2.5.0 評估文 §7 推薦的「open-source SSOT 應為英文」premise 未驗證。Phase 1 pilot 工具（`migrate_ssot_language.py` + dual-mode bilingual lint）保留為 dormant option，不執行也不刪除。**Trigger conditions for re-evaluation**：(1) 收到 ≥3 個非中文母語 contributor PR/issue；(2) 客戶 RFP 顯式要求英文 SSOT；(3) Maintainer 主動 pivot 為 international-positioning project。詳細評估報告（v2.5.0 影響評估 + Phase 1 pilot）全文已歸檔於 closed issue [#145](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/145#issuecomment-4587136920)（status: superseded by S#101 / execution phase cancelled）；trigger 觸發時 reopen #145 取評估依據。

## Pre-commit 品質閘門

79 auto-run + 14 manual-stage + 3 pre-push hooks，清單見 [`.pre-commit-config.yaml`](.pre-commit-config.yaml)。手動觸發：`pre-commit run --all-files`（auto）/ `pre-commit run --hook-stage manual --all-files`（manual）。**hook ↔ skill 職責邊界**（哪些機械強制 / 哪些 AI 須自覺 / 漏接）見 [`hook-vs-skill-coverage.md`](docs/internal/hook-vs-skill-coverage.md)（TRK-304）。

## 文件 / 工具 / Makefile

公開文件對照表 → [`doc-map.md`](docs/internal/doc-map.md)（`docs/internal/**` 由 CLAUDE.md / skills 直接引用，不入 catalog）；Python 工具 → [`tool-map.md`](docs/internal/tool-map.md)（CLI: `da-tools <cmd> --help`）；JSX 工具 SOT → [`tool-registry.yaml`](docs/assets/tool-registry.yaml)。

**Planning / Tracking ID 對照** → [`planning-id-mapping.md`](docs/internal/planning-id-mapping.md)（v2.8.1 起 `TRK-NNN` 為**唯一新進入點**，取代既有 `TECH-DEBT-NNN` / `TD-NN` / `HA-NN` / `REG-NN`；舊 ID 仍可 grep，本表給對映 + 三段編號分區邏輯）。新追蹤項目一律 `TRK-NNN`（commit trailer 寫 `Resolves TRK-NNN`，見 [`dev-rules.md` §P1](docs/internal/dev-rules.md)）；政策依據與 frontmatter spec 見 [ADR-019 §Namespace Policy](docs/adr/019-planning-ssot.md#namespace-policy三-namespace-共存)。`ADR-NNN` 與 `S#NNN` 為獨立 namespace，不參與 TRK 對映。

**Makefile** 必記 Top 11：

- `make pr-preflight` — ⛔ PR merge 前必跑（七項檢查 + 寫 `.git/.preflight-ok.<SHA>` marker）
- `make pre-tag` — ⛔ 打 tag 前必跑（version-check + lint-docs + `docker-build-all` hard gate + `trivy-scan-all` informational；#474 Layer 2，需 docker+trivy）
- `make win-commit MSG=_msg.txt FILES="a b"` — FUSE 卡死時 hook-gated Windows commit（siblings：`make fuse-commit` / `make fuse-locks` / `make recover-index`）
- `make dc-up` / `make dc-test` / `make dc-run CMD="..."` — Dev Container 統一入口
- `make session-cleanup` — session 結束清理
- `make lint-docs` — 一站式文件 lint
- `make platform-data` — 重新產生 Rule Pack 數據
- `make api-docs` — 從 tenant-api swag 標註產生 OpenAPI spec（編輯 handler `@Router`/`@Param` 標註後必跑；CI 有 drift check）
- `make contract-test` — schemathesis 契約測試（build tenant-api + fuzz GET endpoints；TRK-222/TRK-228，CI 已啟用）
- `make portal-build` / `portal-build-watch` — esbuild ESM bundle for portal JSX tools（TRK-230 Option C；entries in `tools/portal/manifest.json`）
- `make test-portal` — Vitest unit tests for portal components（TRK-230）

## Release 流程

六線版號（`v*` / `exporter/v*` / `tools/v*` / `portal/v*` / `recipe-preview/v*` / `tenant-api/v*`）。`recipe-preview/v*` 為「同步升」線（每次平台 release 重 tag、非獨立 cadence——防 bundled-compiler drift；#657 PR-D2）。完整步驟、distribution artifacts、benchmark gate（Phase 1/2/3 rollout）、踩坑記錄見 [`github-release-playbook.md`](docs/internal/github-release-playbook.md)。

## AI Agent 環境

- **Dev Container**: `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container <cmd>`（或 `make dc-run`）；重開機後 `docker start vibe-dev-container` / `make dc-up`
- **K8s MCP** 常 timeout → fallback docker exec；**Prometheus/Alertmanager** `port-forward` + `localhost:9090/9093`
- **測試**: Python tests Cowork VM 直接跑；Go tests 需 Dev Container（`make dc-go-test`）。**檔案清理** `docker exec ... rm -f`（Cowork VM 無法直接 rm 掛載路徑）

任務→Playbook 章節對照（K8s / docker / release / benchmark / E2E 等）→ 觸發 `vibe-playbook-nav` skill。
