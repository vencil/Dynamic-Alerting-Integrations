---
title: "CLAUDE.md — AI 開發上下文指引"
tags: [ai-agent, onboarding, internal]
audience: [ai-agent, maintainers]
version: v2.8.0
lang: zh
---

# CLAUDE.md — AI 開發上下文指引

## ⛔ Agent 起手式（已自動化 🛡️）

Session 起手式 codified 為 **PreToolUse hook** (v2.8.0) — 第一次 `Bash`/`Write`/`Edit`/`MultiEdit` 自動跑 `scripts/session-guards/session-init.py`（關 VS Code Git + 寫 session marker），後續 O(1) no-op。手動觸發 / telemetry / dev-container 啟動 / session 結束清理 → 觸發 `vibe-workflow` skill 或 `make dc-*` / `make session-cleanup`。

第二支 PreToolUse hook：`scripts/session-guards/preflight_bash.py`（audit-2026-04 §H1+H2）— 攔 `sed -i` + 掛載路徑（dev-rules #11，免 token 浪費 fix file hygiene）+ 攔 `_*.bat`/`_*.ps1`/`_*.cmd` 寫到 whitelist 之外（Trap #54 防再造輪子）。被擋時 stderr 直接告訴 Claude 該用什麼替代（Read+Edit / `win_git_escape.bat raw <args>`）。

### 設計原則：主路徑 / 逃生門

> **主路徑**：Dev Container 層做所有事（code / test / commit / push）；優先 `make dc-*` 統一入口。
> **逃生門**：FUSE 卡死時用 Windows 原生 git（`make win-commit` / `scripts/ops/win_git_escape.bat`）。
> **目標**：不讓任何 session 因 FUSE 問題整個卡死。

## ⛔ 高頻地雷（always-on，已升 root；TRK-302）

被燒過 ≥2 次或高頻的規則，從 lazy-load feedback 升為 always-on（不靠 keyword 觸發）：

1. **回應語言** — user-facing prose 一律**繁體中文**（非日文 / 英文）
2. **Commit trailer block** — 所有 trailer 行（`Refs:` / `Self-Review-Pass-2:` / `Co-authored-by:`）須為**最底部單一連續段落、全 `Key: value` 格式**；夾空行或無冒號裸行會劈裂 block → git 丟棄上方行 → CI gate fail（燒過 #515/#522/#543）。多項目 / 純文件 commit 依 [`dev-rules.md` §P1](docs/internal/dev-rules.md) 改在 body prose 列 ID，**不寫 `Resolves` 裸行**
3. **Worktree edit path** — 在 git worktree 內編輯須 anchor worktree 路徑；main repo 同時 checked out，用 main-repo 路徑會悄悄落到 main（燒過 #562）
4. **`git add` 括號 glob** — bash `[01]` 只配 `0`/`1` 不配 `2`；任何括號 glob 後必跑 `git diff --cached --stat` 驗 staged set（燒過 #485 ~2h）
5. **commit / push 前先觸發 `vibe-dev-rules` skill** — pre-commit hook 不攔所有 Vibe gate（如 `make lint-docs-mkdocs`）；skip-and-recover 浪費 2+ push cycle

## Skill 體系

Vibe 專案內建 **六個本地 skills**（`.claude/skills/`），在對應情境自動觸發：

- **`vibe-workflow`** — session 起手式、7 個常見陷阱、標準開發工作流（session 開始或遇到 FUSE / docker / port-forward 類問題時自動觸發）
- **`vibe-dev-rules`** — 12 條開發規範 + Top 4 違反熱點（commit / push / refactor 前自動觸發）
- **`vibe-playbook-nav`** — 任務→Playbook 章節路由（涉及 K8s / docker / release / conf.d / benchmark / E2E 時自動觸發）
- **`vibe-subagent-review`** — IaC-aware 兩階段 review（code 走 spec→quality、IaC 走 blast-radius）（multi-file PR / `Agent` 跑完後、commit 前自動觸發；TRK-305）
- **`vibe-release`** — 五線版號 release 收尾 SOP（make pre-tag → CHANGELOG distill + project-face refresh → 五線 tag → gh release ×5；release 收尾 / phase e 時觸發；TRK-306，延伸 #474 Layer 3）
- **`vibe-brainstorm`** — 設計階段 Socratic ideation（MVP 範圍 / explicit trade-off / defer-with-trigger + 外部 adversarial review；新 ADR / component / epic 拆解 / RFC 時觸發；TRK-308）

環境層 skills（`docx` / `pptx` / `xlsx` / `pdf` / `engineering:*` / `data:*` / `design:*` / `marketing:*` 等）**Claude 可自主判斷使用**，不需逐次徵詢：

- **預設行為**：判斷任務符合 skill 定義時直接讀 SKILL.md 並執行
- **告知方式**：使用前單行說明（例：「跑 `engineering:debug` 的 reproduce 步驟」）
- **多 skill 組合**：一個任務常需多 skill 協作，自主串接
- **新工具發現**：發現該裝但沒裝的 skill，用 `mcp__plugins__search_plugins` / `mcp__mcp-registry__search_mcp_registry` 主動尋找 + 建議

### Skill 優先級宣告（衝突仲裁；TRK-301）

多個 skill 描述同時匹配時，本地 `vibe-*` skills 優先於環境層 generic skills（衝突僅發生在「Vibe 已有專屬流程」範圍）：

- **`vibe-workflow`** supersedes 環境層 session-bootstrap 類指引 — Vibe 起手式已 codified 為 PreToolUse hook
- **`vibe-dev-rules`** supersedes `engineering:code-review` 的 git / commit / branch / trailer 紀律部分 — Vibe 有專屬 12 條規範 + pre-push hook
- **`vibe-playbook-nav`** supersedes 跨 K8s / Helm / release / E2E 的 generic 指引 — Vibe 有專屬 Playbook 章節路由

環境層 skill 仍負責其專業領域（`engineering:debug` reproduce、`data:*` 分析等）；不在「Vibe 已有專屬流程」範圍者照常自主使用。

## 專案概覽

**Multi-Tenant Dynamic Alerting 平台 (v2.8.1)** — Config-driven, SHA-256 hot-reload, Directory Scanner。完整架構速覽見 [architecture-and-design.md](docs/architecture-and-design.md)；版本歷程見 [CHANGELOG.md](CHANGELOG.md)。**v2.8.0 已發版**（2026-05-12，五線 tag 齊發 — 客戶導入管線 + 千租戶 Scale 驗證 + supply-chain provenance；詳 CHANGELOG `## [v2.8.0]`）。其後 **v2.8.1 已發版**（2026-05-16，平台 tag only 的 DX / 內部工具 interim release：文件結構收斂、Planning SSOT、secret-scan 四層防線、Windows MS Store Python 防呆；component binary 與 v2.8.0 相同；詳 CHANGELOG `## [v2.8.1]`）。目前 **v2.9.0 開發中**（ADR-020 tenant federation epic 等；in-flight 工作見 CHANGELOG `## [Unreleased]`）。

## 架構速查

9 個核心設計概念（Severity Dedup / Sentinel Alert / Routing Guardrails / Schema Validation / Cardinality Guard / 三態 / Dual-Perspective / 四層路由 / Tenant API）見 [architecture-and-design.md §設計概念總覽](docs/architecture-and-design.md#設計概念總覽)。spoke 文件在 [`docs/design/`](docs/design/)。

**Secret-scan 四層防線**（v2.8.1 #445）：L0 GitHub native push-protection → L1 pre-commit hook `secrets-scan-staged` → L2 server-side `secret-scan.yml`（不可繞）→ L3 release-time image digest verification。incident response 走 [`secret-leak-remediation-sop.md`](docs/internal/secret-leak-remediation-sop.md)（ASSUME COMPROMISE / ROTATE FIRST）；規範見 [`dev-rules.md` §安全紀律](docs/internal/dev-rules.md)。

## 開發規範（Top 4 熱點）

12 條完整規範見 [`docs/internal/dev-rules.md`](docs/internal/dev-rules.md)；完整 Top 4 說明 + 互動工具變更 SOP → 觸發 `vibe-dev-rules` skill。

1. **#12 Branch + PR** — ⛔ **禁止直推 main**。一律開 branch → PR → owner 同意後 merge。pre-push hook 攔截（`scripts/ops/protect_main_push.sh` + `scripts/ops/require_preflight_pass.sh`）
2. **#11 檔案衛生** — 禁止對掛載路徑用 `sed -i`（會截斷缺少 EOF 換行的檔案）。用 Read+Edit 或 pipe
3. **#4 Doc-as-Code** — 影響 API / schema / CLI / 計數的變更須同步 `CHANGELOG.md` + `CLAUDE.md` + `README.md`
4. **#2 Tenant-Agnostic** — Go / PromQL / fixture 禁止 hardcode tenant id（例如 `db-a`）

## 測試注入 Seam（v2.8.0 後標準）

`ConfigManager` 三個 test-only setter 取代了 v2.7.x 的 global-swap antipatterns。寫新測試前**先確認用對 seam，不要再引入 global swap**：

| 測試要驗 | 用 | 不要再用 |
|---|---|---|
| metrics 寫入 | `freshMetrics(t)` + `m.SetMetrics(fresh)` | ~~`withIsolatedMetrics(t)`~~（已移除） |
| log 輸出 | `log.New(&buf, "", 0)` + `m.SetLogger(testLogger)` | ~~`log.SetOutput(&buf)`~~ |
| WatchLoop / 計時 | `startWatchLoopWithFakeClock(t, m, interval)` + `Advance` + `waitFor(state)` | ~~`time.Sleep` 等 ticker~~ |

完整 patterns + 加 `t.Parallel` 的 RISKY 決策樹 → [`test-map.md` §測試注入 Seam](docs/internal/test-map.md#測試注入-seam-v280-後標準)。`scripts/ops/add_t_parallel.py` 的 RISKY tuple 同時當 lint tripwire；手動加 `t.Parallel` 也要先掃 RISKY。

## 語言策略（SSOT Language）

**Policy locked（v2.8.0 S#101 closure）**：**中文為主 SSOT + 英文為輔**（`foo.md` ZH / `foo.en.md` EN）。**不執行 ZH→EN 遷移**；既有客戶與貢獻者社群均為中文母語，原 v2.5.0 評估文 §7 推薦的「open-source SSOT 應為英文」premise 未驗證。Phase 1 pilot 工具（`migrate_ssot_language.py` + dual-mode bilingual lint）保留為 dormant option，不執行也不刪除。**Trigger conditions for re-evaluation**：(1) 收到 ≥3 個非中文母語 contributor PR/issue；(2) 客戶 RFP 顯式要求英文 SSOT；(3) Maintainer 主動 pivot 為 international-positioning project。詳：[`ssot-language-evaluation.md`](docs/internal/ssot-language-evaluation.md)（status: superseded by S#101）+ [`ssot-migration-pilot-report.md`](docs/internal/ssot-migration-pilot-report.md)（execution phase cancelled）。

## Pre-commit 品質閘門

51 auto-run + 13 manual-stage + 3 pre-push hooks，清單見 [`.pre-commit-config.yaml`](.pre-commit-config.yaml)。手動觸發：`pre-commit run --all-files`（auto）/ `pre-commit run --hook-stage manual --all-files`（manual）。**hook ↔ skill 職責邊界**（哪些機械強制 / 哪些 AI 須自覺 / 漏接）見 [`hook-vs-skill-coverage.md`](docs/internal/hook-vs-skill-coverage.md)（TRK-304）。

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

五線版號（`v*` / `exporter/v*` / `tools/v*` / `portal/v*` / `tenant-api/v*`）。完整步驟、distribution artifacts、benchmark gate（Phase 1/2/3 rollout）、踩坑記錄見 [`github-release-playbook.md`](docs/internal/github-release-playbook.md)。

## AI Agent 環境

- **Dev Container**: `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container <cmd>`（或 `make dc-run`）
- **K8s MCP**: 常 timeout → fallback docker exec
- **Prometheus/Alertmanager**: `port-forward` + `localhost:9090/9093`
- **Python tests**: Cowork VM 可直接跑；Go tests 需在 Dev Container 內（`make dc-go-test`）
- **檔案清理**: `docker exec ... rm -f`（Cowork VM 無法直接 rm 掛載路徑）
- **Dev Container 重啟**: 系統重開機後 `docker start vibe-dev-container` 或 `make dc-up`

任務→Playbook 章節對照（K8s / docker / release / benchmark / E2E 等）→ 觸發 `vibe-playbook-nav` skill。
