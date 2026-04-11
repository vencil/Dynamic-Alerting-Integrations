# CLAUDE.md — AI 開發上下文指引

## ⛔ Agent 起手式（每次 Session 必執行）

> 以下指令在每次 Cowork / Claude Code session 開始時執行，**不分任務類型**。

```bash
python scripts/ops/vscode_git_toggle.py off   # 關閉 VS Code Git 背景操作（防 FUSE phantom lock）
```

如需使用 Dev Container（K8s / Go test / Helm）：

```bash
docker ps | grep vibe-dev-container || docker start vibe-dev-container
```

Session 結束或異常終止後：`make session-cleanup`

> 完整原理見 [windows-mcp-playbook §FUSE Phantom Lock 防治](docs/internal/windows-mcp-playbook.md#fuse-phantom-lock-防治)

### 最常踩的 5 個坑（不用每次讀完整 Playbook）

1. **docker exec stdout 為空** → 用 `> /workspaces/.../_out.txt 2>&1` 重導向再 `cat`（[windows-mcp-playbook §核心原則](docs/internal/windows-mcp-playbook.md)）
2. **FUSE phantom lock** → `make git-preflight`（或 `make git-lock ARGS="--clean"`）；頑強殘影升級 `make fuse-reset`（Level 1+3 自動，Level 2/4/5 指引見 [windows-mcp-playbook §修復層 B](docs/internal/windows-mcp-playbook.md#修復層-bfuse-cache-重建level-1-5)）。FUSE 側 git 操作反覆卡住時，[§修復層 C](docs/internal/windows-mcp-playbook.md#修復層-cwindows-原生-git-fallbackfuse-側卡死時的備援路徑) 提供 Windows 原生 git fallback
3. **PowerShell JSON 中文亂碼** → `[Text.UTF8Encoding]::new($false)` 寫無 BOM（[windows-mcp-playbook #32](docs/internal/windows-mcp-playbook.md)）
4. **pre-commit hook 中斷留下 .git lock** → `make git-lock ARGS="--clean"`，**不要** `--no-verify`
5. **port-forward 殘留佔用端口** → `pkill -f "port-forward.*prometheus"` 或 `make session-cleanup`

## 專案概覽

**Multi-Tenant Dynamic Alerting 平台 (v2.6.0)** — Config-driven, SHA-256 hot-reload, Directory Scanner。完整架構速覽見 [architecture-and-design.md](docs/architecture-and-design.md)；版本歷程見 [CHANGELOG.md](CHANGELOG.md)。

## 架構速查

9 個核心設計概念（Severity Dedup / Sentinel Alert / Routing Guardrails / Schema Validation / Cardinality Guard / 三態 / Dual-Perspective / 四層路由 / Tenant API）見 [architecture-and-design.md §設計概念總覽](docs/architecture-and-design.md#設計概念總覽)。spoke 文件在 [`docs/design/`](docs/design/)。

## 開發規範

11 條專案規範 + 互動工具變更 SOP 見 [`docs/internal/dev-rules.md`](docs/internal/dev-rules.md)。

**最常被違反 Top 3**（其餘請讀完整文件）：

1. **#11 檔案衛生** — 禁止對掛載路徑用 `sed -i`（會截斷缺少 EOF 換行的檔案）。用 Read+Edit 或 `git show HEAD:file | sed | tr -d '\0' > file` pipe
2. **#4 Doc-as-Code** — 影響 API / schema / CLI / 計數的變更須同步 `CHANGELOG.md` + `CLAUDE.md` + `README.md`，連動規則見 [doc-map.md § Change Impact Matrix](docs/internal/doc-map.md)
3. **#2 Tenant-Agnostic** — Go / PromQL / fixture 禁止 hardcode tenant id（例如 `db-a`）

## Pre-commit 品質閘門

30 auto-run + 13 manual-stage hooks。清單見 [`.pre-commit-config.yaml`](.pre-commit-config.yaml)。

```bash
pre-commit run --all-files                        # 全跑 auto hooks
pre-commit run --hook-stage manual --all-files    # manual-stage
```

## 文件 / 工具 / Makefile

- **143 份文件** 對照表 → [`docs/internal/doc-map.md`](docs/internal/doc-map.md)（含受眾、內容摘要、Change Impact Matrix）
- **98 個 Python 工具**（ops 46 / dx 20 / lint 32）→ [`docs/internal/tool-map.md`](docs/internal/tool-map.md)；CLI 速查：`da-tools <cmd> --help`；完整 CLI 參考：[`docs/cli-reference.md`](docs/cli-reference.md)
- **39 個 JSX 互動工具** SOT：[`docs/assets/tool-registry.yaml`](docs/assets/tool-registry.yaml)；變更流程見 [dev-rules.md §互動工具變更 SOP](docs/internal/dev-rules.md#互動工具變更-sop)
- **Makefile** 完整列表：`make help`。必記 Top 4：
  - `make pre-tag` — ⛔ 打 tag 前必跑（version-check + lint-docs）
  - `make session-cleanup` — session 結束清理（vscode-git / lock / port-forward）
  - `make lint-docs` — 一站式文件 lint
  - `make platform-data` — 重新產生 Rule Pack 數據

## Release 流程

五線版號（`v*` / `exporter/v*` / `tools/v*` / `portal/v*` / `tenant-api/v*`）。完整步驟、Distribution artifacts、踩坑記錄見 [`docs/internal/github-release-playbook.md`](docs/internal/github-release-playbook.md)。

## AI Agent 環境

- **Dev Container**: `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container <cmd>`
- **K8s MCP**: 常 timeout → fallback docker exec
- **Prometheus/Alertmanager**: `port-forward` + `localhost:9090/9093`
- **Python tests**: Cowork VM 可直接跑；Go tests 需在 Dev Container 內
- **檔案清理**: `docker exec ... rm -f`（Cowork VM 無法直接 rm 掛載路徑）
- **Dev Container 重啟**: 系統重開機後 `docker start vibe-dev-container`

### Playbook 體系（必讀）

每個操作領域都有對應的 Playbook，記錄累積的經驗、已知陷阱和標準做法。**開始任何非純程式碼的操作前，根據下方任務分流表讀對應 Playbook 章節。**

#### 任務分流表（Agent 起手式）

不讀 Playbook 直接動手是踩坑的主因。以下表格把任務類型映射到必讀的 Playbook **具體章節**（不是整份文件），讓你在 30 秒內找到需要的上下文。

| 任務類型 | 必讀 | 選讀 | 為什麼要讀 |
|---------|------|------|-----------|
| 跑 pytest / 新增測試 | [testing-playbook](docs/internal/testing-playbook.md) 全文 + [test-map](docs/internal/test-map.md) §Factory/Markers | — | 不讀會踩到 fixture 格式、marker 選擇、conftest import 慣例 |
| 跑 benchmark / 效能分析 | [benchmark-playbook](docs/internal/benchmark-playbook.md) 全文 | testing-playbook §負載注入 | 不讀會用錯統計方法或 port-forward 不穩導致數據無效 |
| docker exec / K8s 操作 | [windows-mcp-playbook](docs/internal/windows-mcp-playbook.md) §核心原則 + §已知陷阱 | — | docker exec stdout 在 Windows MCP 下為空，不讀此節會浪費 30 分鐘排錯 |
| Release / 推 tag | [github-release-playbook](docs/internal/github-release-playbook.md) 全文 | windows-mcp-playbook §PowerShell REST API | Re-tag 三輪的教訓全在這裡 |
| 新增 Python 工具 | testing-playbook §SAST 合規 + §程式碼品質 + test-map | — | SAST 7 rules 會在 commit 時攔截不合規的程式碼 |
| 修改 conf.d/ 相關邏輯 | testing-playbook §conf.d/ YAML 格式陷阱 | — | wrapped vs flat 格式是最常踩的坑 |
| 純文件修改 | — | — | pre-commit hooks 自動把關，30 個 hook 涵蓋 drift detection |
| 負載測試 / Alert 驗證 | testing-playbook §負載注入 + §HA 相關測試 | benchmark-playbook §Under-Load | 連線數上限 95 和清理 trap 不做好會鎖死 exporter |
| Playwright E2E | testing-playbook §Playwright E2E | — | server root 必須是 `docs/` 不是 `docs/interactive/` |
| 版號管理 / bump | github-release-playbook §版號驗證 + §da-tools 獨立 Release | — | 五線版號各有各的 tag 格式和 CI 觸發條件 |
| Cowork session 起手式 | [windows-mcp-playbook](docs/internal/windows-mcp-playbook.md) §FUSE Phantom Lock 防治 | — | 不跑 `vscode_git_toggle.py off` 會遭遇 phantom lock，浪費排錯時間 |

#### Playbook 索引

| Playbook / Map | 涵蓋領域 |
|----------------|---------|
| [`testing-playbook.md`](docs/internal/testing-playbook.md) | K8s 排錯、負載注入、程式碼品質、SAST、Playwright E2E |
| [`benchmark-playbook.md`](docs/internal/benchmark-playbook.md) | Benchmark 方法論、執行環境、踩坑記錄 |
| [`test-map.md`](docs/internal/test-map.md) | 測試架構：factories、markers、檔案對照、snapshot 工作流 |
| [`windows-mcp-playbook.md`](docs/internal/windows-mcp-playbook.md) | Docker exec、Shell 陷阱、Port-forward、Helm 防衝突、PowerShell 環境 |
| [`github-release-playbook.md`](docs/internal/github-release-playbook.md) | Git push、Tag、GitHub Release、CI 觸發、PAT 權限 |

### Playbook 維護原則

Playbook 是 **living documents**，跟隨專案演進持續更新：

1. **Lesson Learned 回寫**：每次遇到新陷阱或發現更好做法，立即更新對應 Playbook（不是下次再說）
2. **知識退火**：LL 跨越兩個 minor 版本時強制三選一——固化為正式規範 / 標記 🛡️ 已自動化 / 歸檔至 `archive/`。`make playbook-freshness` 自動檢查各 Playbook 的 `verified-at-version` 欄位
3. **交叉引用**：Playbook 之間用相對連結互相引用，避免重複內容。環境層陷阱統一在 windows-mcp-playbook 維護
4. **全局 vs 領域**：CLAUDE.md 只放指引級摘要（指向哪個 Playbook），詳細步驟和陷阱清單放 Playbook 內
5. **驗證更新**：Playbook 內的數字（Rule Pack 數量、工具數量等）在版本升級時一併更新

### 開發 Session 標準工作流

1. **起手式**：執行上方 [§Agent 起手式](#agent-起手式每次-session-必執行) → 根據任務分流表讀相關 Playbook
2. **開發**：程式碼修改 → Go test / Python test → 場景驗證
3. **Benchmark**：完整 benchmark（idle + routing + Go micro-bench）→ 記錄到 CHANGELOG + architecture docs
4. **文件同步**：`bump_docs.py --check` → 更新 CLAUDE.md / README / CHANGELOG 的計數
5. **Commit**：`git commit` → pre-commit hooks 自動執行 13 個品質檢查（platform-data drift, tool consistency, versions...）+ 6 個 manual-stage hooks
6. **Lesson Learned**：回寫 Playbook + CLAUDE.md

## 長期展望
