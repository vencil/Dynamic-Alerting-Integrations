---
title: "CLAUDE.md — AI 開發上下文指引"
tags: [ai-agent, onboarding, internal]
audience: [ai-agent, maintainers]
version: v2.7.0
lang: zh
---

# CLAUDE.md — AI 開發上下文指引

## ⛔ Agent 起手式（已自動化 🛡️）

Session 起手式已 codified 為 **PreToolUse hook**（v2.8.0）— 第一次 `Bash`/`Write`/`Edit`/`MultiEdit` 呼叫自動跑 `scripts/session-guards/session-init.py`（關 VS Code Git 背景操作 + 寫 session marker），後續同 session 呼叫 O(1) no-op。Session 用 `CLAUDE_SESSION_ID` 區分。

- **手動觸發**（偵錯）：`python scripts/session-guards/session-init.py [--status|--force|--stats]`
- **Telemetry**：每次 hook 呼叫 append JSON Lines 到 `~/.cache/vibe/session-init.log`（Windows `%LOCALAPPDATA%\vibe\`）。`--stats` 印 counts / sessions / last N events；`--stats --json` 供 jq pipe；`VIBE_SESSION_LOG=/dev/null` 停用。
- **Dev Container**：`make dc-up` / `make dc-test` / `make dc-run CMD="..."`
- **Session 結束**：`make session-cleanup`

詳細起手式 / 7 個坑 / 標準工作流 → 觸發 `vibe-workflow` skill。

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

**Multi-Tenant Dynamic Alerting 平台 (v2.7.0)** — Config-driven, SHA-256 hot-reload, Directory Scanner。完整架構速覽見 [architecture-and-design.md](docs/architecture-and-design.md)；版本歷程見 [CHANGELOG.md](CHANGELOG.md)。v2.8.0 仍在開發中。

## 架構速查

9 個核心設計概念（Severity Dedup / Sentinel Alert / Routing Guardrails / Schema Validation / Cardinality Guard / 三態 / Dual-Perspective / 四層路由 / Tenant API）見 [architecture-and-design.md §設計概念總覽](docs/architecture-and-design.md#設計概念總覽)。spoke 文件在 [`docs/design/`](docs/design/)。

## 開發規範（Top 4 熱點）

12 條完整規範見 [`docs/internal/dev-rules.md`](docs/internal/dev-rules.md)；完整 Top 4 說明 + 互動工具變更 SOP → 觸發 `vibe-dev-rules` skill。

1. **#12 Branch + PR** — ⛔ **禁止直推 main**。一律開 branch → PR → owner 同意後 merge。pre-push hook 攔截（`scripts/ops/protect_main_push.sh` + `scripts/ops/require_preflight_pass.sh`）
2. **#11 檔案衛生** — 禁止對掛載路徑用 `sed -i`（會截斷缺少 EOF 換行的檔案）。用 Read+Edit 或 pipe
3. **#4 Doc-as-Code** — 影響 API / schema / CLI / 計數的變更須同步 `CHANGELOG.md` + `CLAUDE.md` + `README.md`
4. **#2 Tenant-Agnostic** — Go / PromQL / fixture 禁止 hardcode tenant id（例如 `db-a`）

## 語言策略（SSOT Language）

**現況（v2.7.0）**：中文為主 SSOT + 英文為輔。文件對為 `foo.md`（ZH）+ `foo.en.md`（EN）。

**目標（v2.8.0）**：英文為主 SSOT + 中文為輔。文件對將為 `foo.md`（EN）+ `foo.zh.md`（ZH）。

**遷移狀態**：Phase 1（工具準備）已完成，Phase 2（全量遷移）排定 v2.8.0。

- 遷移腳本：`scripts/tools/dx/migrate_ssot_language.py`（`--dry-run` / `--execute --git`）
- Lint hooks 已支援 `.en.md` 和 `.zh.md` 雙模式（auto-detect）
- 完整評估：[`docs/internal/ssot-migration-pilot-report.md`](docs/internal/ssot-migration-pilot-report.md)

## Pre-commit 品質閘門

31 auto-run + 13 manual-stage hooks。清單見 [`.pre-commit-config.yaml`](.pre-commit-config.yaml)。

```bash
pre-commit run --all-files                        # 全跑 auto hooks
pre-commit run --hook-stage manual --all-files    # manual-stage
```

## 文件 / 工具 / Makefile

- **114 份公開文件** 對照表 → [`docs/internal/doc-map.md`](docs/internal/doc-map.md)（`docs/internal/**` 不入 catalog；那些是 maintainer / AI agent 用的 playbook/planning，由 CLAUDE.md / skills 直接引用）
- **122 個 Python 工具** → [`docs/internal/tool-map.md`](docs/internal/tool-map.md)；CLI 速查：`da-tools <cmd> --help`
- **39 個 JSX 互動工具** SOT：[`docs/assets/tool-registry.yaml`](docs/assets/tool-registry.yaml)
- **Makefile** 必記 Top 7：
  - `make pr-preflight` — ⛔ PR merge 前必跑（七項檢查 + 寫 `.git/.preflight-ok.<SHA>` marker）
  - `make pre-tag` — ⛔ 打 tag 前必跑（version-check + lint-docs）
  - `make win-commit MSG=_msg.txt FILES="a b"` — FUSE 卡死時 hook-gated Windows commit（siblings：`make fuse-commit` plumbing 逃生門 / `make fuse-locks` 幻影鎖診斷 / `make recover-index` 重建 `.git/index`）
  - `make dc-up` / `make dc-test` / `make dc-run CMD="..."` — Dev Container 統一入口
  - `make session-cleanup` — session 結束清理
  - `make lint-docs` — 一站式文件 lint
  - `make platform-data` — 重新產生 Rule Pack 數據

## Release 流程

五線版號（`v*` / `exporter/v*` / `tools/v*` / `portal/v*` / `tenant-api/v*`）。完整步驟、Distribution artifacts、踩坑記錄見 [`docs/internal/github-release-playbook.md`](docs/internal/github-release-playbook.md)。

**Pre-tag 加跑 `make benchmark-report`（v2.8.0, issue #60 Phase 1）**：1000-tenant baseline 寫到 `.build/bench-baseline.txt`，informational only（pre-tag 不阻擋）。Tag 前人眼比上次 trend；異常時延遲 tag 並先在 issue #60 留 comment。Nightly `bench-record` workflow（main only, 90 天 retention）累積 ~28 點數據後評估 Phase 2 hard gate（3× median-of-5）。

## AI Agent 環境

- **Dev Container**: `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container <cmd>`（或 `make dc-run`）
- **K8s MCP**: 常 timeout → fallback docker exec
- **Prometheus/Alertmanager**: `port-forward` + `localhost:9090/9093`
- **Python tests**: Cowork VM 可直接跑；Go tests 需在 Dev Container 內（`make dc-go-test`）
- **檔案清理**: `docker exec ... rm -f`（Cowork VM 無法直接 rm 掛載路徑）
- **Dev Container 重啟**: 系統重開機後 `docker start vibe-dev-container` 或 `make dc-up`

任務→Playbook 章節對照（K8s / docker / release / benchmark / E2E 等）→ 觸發 `vibe-playbook-nav` skill。
