---
title: "CLAUDE.md — AI 開發上下文指引"
tags: [ai-agent, onboarding, internal]
audience: [ai-agent, maintainers]
version: v2.7.0
lang: zh
---

# CLAUDE.md — AI 開發上下文指引

## ⛔ Agent 起手式（已自動化 🛡️）

Session 起手式 codified 為 **PreToolUse hook** (v2.8.0) — 第一次 `Bash`/`Write`/`Edit`/`MultiEdit` 自動跑 `scripts/session-guards/session-init.py`（關 VS Code Git + 寫 session marker），後續 O(1) no-op。手動觸發 / telemetry / dev-container 啟動 / session 結束清理 → 觸發 `vibe-workflow` skill 或 `make dc-*` / `make session-cleanup`。

### 設計原則：主路徑 / 逃生門

> **主路徑**：Dev Container 層做所有事（code / test / commit / push）；優先 `make dc-*` 統一入口。
> **逃生門**：FUSE 卡死時用 Windows 原生 git（`make win-commit` / `scripts/ops/win_git_escape.bat`）。
> **目標**：不讓任何 session 因 FUSE 問題整個卡死。

## Skill 體系

Vibe 專案內建 **三個本地 skills**（`.claude/skills/`），在對應情境自動觸發：

- **`vibe-workflow`** — session 起手式、7 個常見陷阱、標準開發工作流（session 開始或遇到 FUSE / docker / port-forward 類問題時自動觸發）
- **`vibe-dev-rules`** — 12 條開發規範 + Top 4 違反熱點（commit / push / refactor 前自動觸發）
- **`vibe-playbook-nav`** — 任務→Playbook 章節路由（涉及 K8s / docker / release / conf.d / benchmark / E2E 時自動觸發）

環境層 skills（`docx` / `pptx` / `xlsx` / `pdf` / `engineering:*` / `data:*` / `design:*` / `marketing:*` 等）**Claude 可自主判斷使用**，不需逐次徵詢：

- **預設行為**：判斷任務符合 skill 定義時直接讀 SKILL.md 並執行
- **告知方式**：使用前單行說明（例：「跑 `engineering:debug` 的 reproduce 步驟」）
- **多 skill 組合**：一個任務常需多 skill 協作，自主串接
- **新工具發現**：發現該裝但沒裝的 skill，用 `mcp__plugins__search_plugins` / `mcp__mcp-registry__search_mcp_registry` 主動尋找 + 建議

## 專案概覽

**Multi-Tenant Dynamic Alerting 平台 (v2.7.0)** — Config-driven, SHA-256 hot-reload, Directory Scanner。完整架構速覽見 [architecture-and-design.md](docs/architecture-and-design.md)；版本歷程見 [CHANGELOG.md](CHANGELOG.md)。**v2.8.0 開發中**：Phase .a/.b/.c 已完成（客戶導入管線：MetricsQL parser → Profile Builder → Hierarchy-aware Batch PR → Dangling Defaults Guard + Migration Toolkit cosign+SBOM）；Phase .d ZH-primary policy lock；Phase .e prep 中（da-portal 5-PR + tenant-api 11-PR 多波 refactor sweeps lifting code quality before tag；tenant-api 11-PR sweep complete），release 收尾（4-hr soak / pre-tag / 五線 tag）尚未啟動。

## 架構速查

9 個核心設計概念（Severity Dedup / Sentinel Alert / Routing Guardrails / Schema Validation / Cardinality Guard / 三態 / Dual-Perspective / 四層路由 / Tenant API）見 [architecture-and-design.md §設計概念總覽](docs/architecture-and-design.md#設計概念總覽)。spoke 文件在 [`docs/design/`](docs/design/)。

## 開發規範（Top 4 熱點）

12 條完整規範見 [`docs/internal/dev-rules.md`](docs/internal/dev-rules.md)；完整 Top 4 說明 + 互動工具變更 SOP → 觸發 `vibe-dev-rules` skill。

1. **#12 Branch + PR** — ⛔ **禁止直推 main**。一律開 branch → PR → owner 同意後 merge。pre-push hook 攔截（`scripts/ops/protect_main_push.sh` + `scripts/ops/require_preflight_pass.sh`）
2. **#11 檔案衛生** — 禁止對掛載路徑用 `sed -i`（會截斷缺少 EOF 換行的檔案）。用 Read+Edit 或 pipe
3. **#4 Doc-as-Code** — 影響 API / schema / CLI / 計數的變更須同步 `CHANGELOG.md` + `CLAUDE.md` + `README.md`
4. **#2 Tenant-Agnostic** — Go / PromQL / fixture 禁止 hardcode tenant id（例如 `db-a`）

## 語言策略（SSOT Language）

**Policy locked（v2.8.0 S#101 closure）**：**中文為主 SSOT + 英文為輔**（`foo.md` ZH / `foo.en.md` EN）。**不執行 ZH→EN 遷移**；既有客戶與貢獻者社群均為中文母語，原 v2.5.0 評估文 §7 推薦的「open-source SSOT 應為英文」premise 未驗證。Phase 1 pilot 工具（`migrate_ssot_language.py` + dual-mode bilingual lint）保留為 dormant option，不執行也不刪除。**Trigger conditions for re-evaluation**：(1) 收到 ≥3 個非中文母語 contributor PR/issue；(2) 客戶 RFP 顯式要求英文 SSOT；(3) Maintainer 主動 pivot 為 international-positioning project。詳：[`ssot-language-evaluation.md`](docs/internal/ssot-language-evaluation.md)（status: superseded by S#101）+ [`ssot-migration-pilot-report.md`](docs/internal/ssot-migration-pilot-report.md)（execution phase cancelled）。

## Pre-commit 品質閘門

39 auto-run + 14 manual-stage + 3 pre-push hooks，清單見 [`.pre-commit-config.yaml`](.pre-commit-config.yaml)。手動觸發：`pre-commit run --all-files`（auto）/ `pre-commit run --hook-stage manual --all-files`（manual）。

## 文件 / 工具 / Makefile

公開文件對照表 → [`doc-map.md`](docs/internal/doc-map.md)（`docs/internal/**` 由 CLAUDE.md / skills 直接引用，不入 catalog）；Python 工具 → [`tool-map.md`](docs/internal/tool-map.md)（CLI: `da-tools <cmd> --help`）；JSX 工具 SOT → [`tool-registry.yaml`](docs/assets/tool-registry.yaml)。

**Makefile** 必記 Top 7：

- `make pr-preflight` — ⛔ PR merge 前必跑（七項檢查 + 寫 `.git/.preflight-ok.<SHA>` marker）
- `make pre-tag` — ⛔ 打 tag 前必跑（version-check + lint-docs）
- `make win-commit MSG=_msg.txt FILES="a b"` — FUSE 卡死時 hook-gated Windows commit（siblings：`make fuse-commit` / `make fuse-locks` / `make recover-index`）
- `make dc-up` / `make dc-test` / `make dc-run CMD="..."` — Dev Container 統一入口
- `make session-cleanup` — session 結束清理
- `make lint-docs` — 一站式文件 lint
- `make platform-data` — 重新產生 Rule Pack 數據

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
