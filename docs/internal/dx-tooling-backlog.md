---
title: DX Tooling Backlog
tags: [dx, tooling, backlog, internal]
audience: [maintainers, contributors]
lang: zh
version: v2.7.0
---

# DX Tooling Backlog

> Forward-looking DX 改善追蹤。已完成的項目見 [CHANGELOG.md](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CHANGELOG.md)。
> 與產品 Roadmap（`architecture-and-design.md` §5）分開管理：§5 放核心功能方向，這裡放 DX / 內部工具 / 測試品質。

## 狀態說明

- **候選** — 已識別但尚未排入
- **進行中** — 當前 iteration 正在實作

---

## 候選 — DX 工具改善

### win-escape-helpers PR #29 follow-ups

**來源**：PR #29（`chore/sandbox-hook-runner`）code review 的七點建議裡，除了 #1/#2（已在同 PR 內修完 doc label 對齊）以外的五個非阻擋項目。全部屬於 polish / 韌性增強，不影響當前 `make win-commit` 正確性。

1. **`win_async_exec.ps1` `IsPathRooted "Illegal characters in path"` bug** — PR #29 dogfood 階段發現，傳入相對或絕對 `LogFile` 路徑都會觸發此錯。當下 workaround 是直接跑 `scripts/ops/win_git_escape.bat push`（小 push 秒殺，MCP timeout 風險低）。修法方向：helper 內 `Resolve-Path -LiteralPath`，並明確規定呼叫方用 forward slash 或規範化路徑。
2. **Makefile `win-commit` 的 `$(FILES)` 未 quote**（Makefile L196）— 檔名含空格會斷在 cmd.exe 字串裡。repo 目前路徑沒有空格所以沒炸過，但不是 regression-safe。解法：在呼叫 `win_git_escape.bat add` 前先組 array 或用 `printf %q`。
3. **`_sandbox_hooks.log` 加 explicit `.gitignore` entry**（`run_hooks_sandbox.sh` L79）— 目前靠 `*.log` wildcard 涵蓋，未來若 `.gitignore` 重構把 wildcard 拿掉就會裸奔。補一條 explicit 的防守線；或者把 log 改寫到 `$REPO_ROOT/.git/_sandbox_hooks.log`（永不會被 track）。
4. **`run_hooks_sandbox.sh` repo-外絕對路徑的友善錯誤訊息**（L74）— 目前 `case /* → FILES+=("$f")` 直接丟給 pre-commit，回的錯誤訊息不太直覺。加一個前置檢查 `warn if ! -e "$REPO_ROOT/$rel"` 能提升 UX。
5. **`run_hooks_sandbox.sh` REPO_ROOT 解析韌性**（L57）— 目前硬寫 `$SCRIPT_DIR/../..`，script 若以後移出 `scripts/ops/` 會默默壞掉。加 `git rev-parse --show-toplevel` fallback。
6. **Makefile `SKIP` env var 雙重設定**（L198）— `SKIP="$(SKIP)" $$CMD_EXE /c "set SKIP=$(SKIP)&& ..."` 一次在 Linux shell、一次在 cmd 字串內。belt-and-suspenders，留 inline `set SKIP=` 即可（Linux→cmd 邊界的 env 繼承不一定穩）。

**時程**：無 urgency，可搭下一次 win-escape 相關的 session 一起處理。`#1`（`win_async_exec.ps1` bug）實際使用時會再撞到，優先級略高於其他。

### check_doc_freshness.py Helm chart 版號檢查

目前只檢查 Docker image 版號，擴展到 `helm install/upgrade` 命令中的 `--version` flag 比對。

### validate_all.py `--fix --diff` combo

`--fix` 後自動顯示 diff summary（目前 `--fix` 和 `--diff-report` 是獨立的），減少手動操作步驟。

### generate_doc_map.py ADR 預設包含

目前需要 `--include-adr` 才會包含 ADR 文件。考慮改為預設行為（或 `--exclude-adr` 反向控制），減少遺漏風險。pre-commit hook 已加 `--include-adr`，但 CLI 預設值不一致。

### Metric Dictionary 自動驗證

`metric-dictionary.yaml` 的 metric 名稱應與 Rule Pack YAML 中實際使用的 recording rule 交叉驗證。偵測字典中存在但 Rule Pack 不使用的 stale entry，和 Rule Pack 使用但字典未收錄的 undocumented metric。

### Alert Preview Tab 大值指標對數刻度

slider max = `threshold * 2` 對大數值指標（如 Kafka lag 200000）太粗。當 threshold > 10000 時自動切換為對數刻度，提升細粒度調整體驗。

### parseYaml 替換為 js-yaml

Self-Service Portal 的 regex-based YAML parser 功能受限（不支援多行字串、anchor 等）。替換為 CDN 載入 `js-yaml` library 可一次性解決所有 edge case。需測試 Babel standalone 環境下的 ES module 相容性。

### CLI help 一致性 lint

92+ 個 Python 工具的 argparse help text 格式標準化：統一使用動詞開頭、統一中英文語言（依 `detect_cli_lang()`）、統一 metavar 命名風格。

### check_md_cli_drift.py — Markdown CLI 參數防漂移

掃描 markdown 中 `bash` code block 裡以 `da-tools` 開頭的指令，驗證參數是否仍合法。前置條件：需先建立 CLI parameter registry（類似 `tool-registry.yaml` 對 JSX 工具的做法），目前 92 個工具的 argparse 定義散落在各自腳本中，無統一 machine-readable schema。**建議等 CLI parameter registry 建立後再實作。**

### check_bilingual_structure.py 升級：章節骨架比對

v2.6.0-final 發現 troubleshooting.en.md 缺少 Operator 章節（直到 final review 才被發現）。現有工具只檢查檔案存在性。升級為比對 `##` / `###` heading 數量與層級，偵測章節級內容漂移。

### ZH/EN 文件骨架 drift 對齊修正（一次性清償）

**來源**：PR #21 (`chore/structure-cleanup-2026-04-11`) push 階段發現。當時 `.pre-commit-config.yaml` 未設 `default_stages`，`bilingual-structure-check` 意外在 `git push` 階段被觸發全 repo 掃描，回報 **23 errors + 18 warnings + ~95 nav issues** 散佈在 62 對 `*.md` / `*.en.md` 檔案。drift 類型包含：
- 章節數量不對齊（多/少 `##` / `###` heading）
- heading 層級錯位（ZH 用 `##` EN 用 `###`）
- nav/TOC 連結對應缺失

**為什麼是 backlog 而非即時修**：PR #21 範圍是結構清理，混入大量 docs 編輯會稀釋 commit 意圖 + 難 review。當時採用 `default_stages: [pre-commit]` 把 hook 從 pre-push 移除（見 `.pre-commit-config.yaml` 2026-04-12 commit + `windows-mcp-playbook.md` §修復層 D Layer 3），**沒有**降低 hook 嚴格度或切 `stages: [manual]`——drift 仍會在 `pre-commit run --all-files` 和 CI 上被抓到。

**採用路線：對齊修正（不是例外清單）**。理由：
1. 新增 `bilingual-drift-allowlist.yaml` 會讓 false-positive 與「還沒修」變得無法區分，長期會累積成「第二份事實來源」
2. 對齊修正是 doc-as-code 原則的直接延伸（dev-rules #4），本來就該做
3. 修完後可以真正信任 `bilingual-structure-check` 當 gate，而不是「有 23 個是白名單，新增第 24 個才擋」

**執行計畫**（獨立 PR，不混其他變更）：
1. 用 `check_bilingual_structure.py --ci` 產出完整 error list，按檔案分組
2. 分批修正（每批 5~10 對檔案），每批一個 commit，commit message 標註修的 drift 類型
3. 修完跑 `pre-commit run bilingual-structure-check --all-files` + `check_bilingual_structure.py` 升級版（若骨架比對已先做）驗證乾淨
4. 驗收門檻：pre-commit + CI 完全綠 + error count == 0

**前置依賴**：上方「check_bilingual_structure.py 升級：章節骨架比對」完成後再做更理想——對齊修正時可以一次把 heading 層級也修對齊，避免「先修 42 個 error，升級工具後又發現 15 個新 error」。若時程壓力下先做也可，只是後續可能補一次。

**工作量估算**：62 對檔案 × 平均 2 個 drift 點 ≈ 2~3 個工作日。可拆成 5~8 個 PR 分批合入。

---

## 候選 — 互動工具

### Config Diff Visualizer (JSX) — 完整版

v2.2.0 在 Routing Trace Tab 加入 Copy JSON 匯出。完整版需要獨立工具：side-by-side 比較兩個 tenant 或兩個版本的 YAML 配置差異，利用 `config-diff --json` 輸出渲染 diff view（新增/移除/修改 highlight）。

### Playwright E2E CI 環境修復

v2.6.0 的 `playwright.yml` workflow 在 CI 持續失敗，因為 Portal 需要 static file server 但 CI 環境沒有啟動。需要選擇一種方案讓 E2E 在 CI 跑綠：

- **方案 A（推薦）**：CI 中用 `npx serve docs/` 或 `python -m http.server` 做輕量 static serve，workflow 已有 30s retry loop 可對接
- **方案 B**：用 `da-portal` Docker image 當 GitHub Actions service container（`services:` block）
- **方案 C**：將 workflow 標記 `continue-on-error: true`，僅作為 advisory check 不阻塞 merge

驗收標準：Playwright workflow 在 push to main 時穩定通過（或明確標記為 non-blocking）。

### Interactive Tool Playwright E2E — 完整覆蓋

v2.6.0 已整合 axe-core 自動化無障礙測試至 6 個 spec files（39+ test cases），含 Operator Setup Wizard 12 tests。擴展至覆蓋全部 42 支 JSX 工具：每支工具可載入、基本互動正常、無 console error、i18n 切換不破版。前置條件：先完成上方 CI 環境修復。

### Cost Estimator 動態連動 platform-data.json

v2.6.0 Phase .d-R2 建議：pack count slider 未連動 platform-data.json。改為啟動時 fetch platform-data.json，slider max 動態調整。

### Notification Editor 即時字元計數器

v2.6.0 Phase .d-R2 建議：template editing 時顯示即時 char counter，幫助使用者掌握各 receiver 的長度限制（Slack 3000 / Email 無限 / PagerDuty 1024）。

### deployment-wizard + alert-builder Step component 抽取（PR-portal-11 follow-up）

PR-portal-11 ([#213](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/213)) 在 4 個 multi-step / multi-tab 工具加上 subtree ErrorBoundary，唯獨 `deployment-wizard.jsx` (504 LOC) + `alert-builder.jsx` (616 LOC) 跳過：兩者 step content 都是 inline JSX (`{currentStep.id === 'tier' && (<div>...</div>)}` × 6 / `{step === N && (<div>...</div>)}` × 4)，不是抽出來的 Step component；直接包 boundary 會變多層 nesting + 條件交錯，醜且難 review。

兩階段執行（建議放同一個 PR）：

**Phase A — 抽 Step components**。每個 inline block 抽到 sibling `<wizard>/components/Step*.jsx`（deployment 6 個、alert-builder 4 個），符號用 `window.__DeployStepX` / `window.__AlertBuilderStepX` 前綴避免跨工具衝突（同 PR-portal-10 的 `__CICD_*` / `__DEPLOY_*` / `__RBAC_*` 命名慣例）。orchestrator 的 conditional render 變乾淨：`{currentStep.id === 'tier' && <StepTier {...stepProps} />}`。

**Phase B — 套 boundary**。複製 PR-portal-11 的 cicd-setup-wizard pattern：`<ErrorBoundary key={currentStep.id} scope={'deployment-wizard/step/' + currentStep.id}>{...所有 conditionals...}</ErrorBoundary>`。1 個 wrap 配 `key={...id}` 每步換新 mount。

**兩個必踩坑**：

- **`auth` step 的雙條件** (`currentStep.id === 'auth' && config.tier === 'tier2'`) — `config.tier === 'tier2'` 邏輯**必須留在 orchestrator**（決定 step 是否出現），不要搬進 `StepAuth`；否則 step nav 顯示這個 step 但 component 內 early-return → 一片空白卡關。
- **sed-damage threshold**：deployment-wizard 抽完 504 → ~150 LOC（70% shrink）+ alert-builder 616 → ~200 LOC（67% shrink）→ 兩個都觸發 `sed-damage-guard`。需把兩個路徑加進 `.sed-damage-allowlist`（PR-portal-9 #211 portal-shared.jsx 已有先例 + PR-portal-6 #208 機制本身）。

**預估規模**：10 個新檔 + 2 個 orchestrator 改動 + 2 個 allowlist 條目 ≈ -770 LOC orchestrator 總減量；單一 PR ~800 LOC delta（reviewable，跟 PR-portal-10 同級）。

**時程：post-v2.8.0**。當前兩個工具功能正常（loader-level boundary from PR-portal-2 仍能接全工具失敗），不是 bug 是 incremental quality。塞進 v2.8.0 release window 會讓 release notes 變雜（已 11 個 portal PR）。若 ship 後客戶遇到「某 step 壞掉拖死整個 wizard」事件再提前。

---

## 候選 — 測試品質

### Snapshot test 擴展

為 `generate_report()` / `generate_markdown()` / `print_text_report()` 輸出加入 JSON snapshot 穩定性測試，防止格式意外變更。v2.3.0 已建立 snapshot 基礎設施，可擴展覆蓋範圍。

### test docstring 覆蓋率 lint

新增 pre-commit hook 或 lint script，自動檢查所有 test method 是否有繁體中文 docstring。確保測試意圖一目了然。

### Property-based testing 進一步擴展

v2.6.0 已新增 22 個 Hypothesis tests（RFC 1123, SHA-256, drift, YAML round-trip, kustomization）。進一步擴展方向：將核心 property tests（如 `parseMetricKey` 的 idempotency、`clampDuration` 的 monotonicity）加入。考慮 Go 端的 rapid/gopter property testing。

### Go test `-race` CI 預設化

v2.6.0 Phase .e 發現 3 個 data race（直到 `-race` flag 才偵測到）。v2.7.0 Dev Container Block 已將 exporter + tenant-api 兩組 race test 跑綠（task #9、#10），但 `ci.yml` 預設仍不帶 `-race`。應將 `go test -race` 設為 CI required job（可以 matrix 分 race/no-race 兩軌，保留 no-race 作快速回饋）。

---

## 候選 — Harness Audit v2.7.0 發想（已歸檔）

> 來源：Harness Audit v2.7（local-only，不進 repo）第一輪 + 第二輪 review。
> 這些項目聚焦「防守工具本身的品質和可信度」，與上方測試品質的差異在於：上方是測產品程式碼的測試，這裡是測「測試工具」的測試。
>
> **v2.7.0 進度**：HA-6 的 `make test-skip-audit` target 已在 v2.7.0 落地（Makefile L387，BUDGET=5），但 CI gate 尚未接上。其他項目 HA-1~HA-5、HA-7~HA-9 仍為候選，延續至 v2.8.0。

### HA-1: `check_noqa_hygiene.py` — noqa/nosec 必要性驗證

114 個 `# noqa: E402` 目前合理，但隨著程式碼重構 import 順序可能變化，noqa 卻不會自動移除。新增 lint 對每個 noqa 做反向驗證：暫時移除 noqa 後用 ruff/flake8 跑一次，如果不報錯就代表 noqa 已不必要。同理適用於 `# nosec` 和 `# type: ignore`。

**驗收**：`check_noqa_hygiene.py --ci` exit 0 + 至少消除 5 個不必要的 noqa。

### HA-2: `make test-impact` — 變更影響測試自動縮減

目前 4232 tests 全跑 ~40s，尚可接受。但隨著成長到 8000+ tests 需要基於 `git diff` 的 test impact analysis：改了哪個 module → 只跑對應的 test files。可用 `pytest-testmon` 或自建 import graph analysis（`ast` 掃描 `import` 語句建立 module → test 映射表）。

**前置**：需先建立 module → test 映射表（或直接整合 `pytest-testmon`）。
**驗收**：`make test-impact` 在只改 1 支工具時執行時間 <10s。

### HA-3: Pre-commit hook CI gate

目前 pre-commit hooks 只在本地 commit 時跑。如果 agent 用了 `--no-verify`（雖然規則禁止），CI 不會補跑 `pre-commit run --all-files`。新增 CI job 跑全量 pre-commit hooks 作為 PR required check。

**注意**：`ci.yml` 的 lint job 已跑部分 hooks，但不是全量。差異：bilingual-structure-check、jsx-i18n-check、doc-links-check 等 auto hooks 在 CI 未列出。

**驗收**：CI 有一個 job 跑 `pre-commit run --all-files`，作為 required check。

### HA-4: Lint tool self-test framework（negative fixtures）

每支 lint tool 目前靠對整個 repo 跑 `--ci` 來驗證。但如果 repo 碰巧符合所有規則，lint 的 error detection 邏輯從未被測試。例如 `check_bilingual_content.py` 在 repo 全 pass 時，我們無法確認它真的能偵測到 CJK ratio 超標。

**方案**：建立 `tests/fixtures/lint-negative/` 目錄，放故意有 error 的檔案（如一個 .en.md 含 50% CJK），每支 lint tool 的 pytest 用這些 fixtures 確認能正確偵測問題。

**驗收**：每支 auto hook 的 lint tool 至少有 1 個 negative fixture test。

### HA-5: `check_test_isolation.py` — 測試隔離驗證

跑 `pytest --randomly` 驗證 tests 之間沒有隱式依賴（例如 test A 寫了檔案、test B 依賴它存在）。防止「順序跑全過，亂序跑就 fail」的隱藏耦合。

**前置**：安裝 `pytest-randomly`。
**驗收**：`pytest --randomly -x` 連跑 3 次（不同 seed）全部通過。

### HA-6: Skip budget CI gate

目前 `make test-skip-audit`（本次審計新增）是 local target。升級為 CI step：在 `python-tests` job 末尾加一個 step，assert skip count ≤ budget (5)。防止 skip 數量因新增依賴性 skip 而悄悄膨脹回去。

**注意**：budget 值需與 `make test-skip-audit` 的 `BUDGET=5` 一致。如果 CI 環境和 local 的 skip count 不同（因依賴差異），需分別設定。

**驗收**：CI 中有 `test-skip-audit` step，skip count > budget 時 job fail。

### HA-7: Lint test coverage 補齊（18 支缺測試）

Harness Audit R2 發現 33 支 lint 工具中 18 支沒有對應 `test_*.py`。優先補 auto hook 中的高風險工具：

| 優先級 | 工具 | 行數 | 風險 |
|--------|------|------|------|
| P0 | `validate_docs_versions.py` | 1,089 | 最大 lint 工具，邏輯複雜 |
| P0 | `check_head_blob_hygiene.py` | 447 | 安全底線工具 |
| P1 | `lint_jsx_babel.py` | 302 | auto hook，Babel 相容性 |
| P1 | `check_bilingual_structure.py` | 371 | auto hook |
| P1 | `check_jsx_i18n.py` | 291 | auto hook |
| P1 | `check_metric_dictionary.py` | 233 | auto hook |
| P2 | `check_build_completeness.py` | 133 | auto hook |
| P2 | `detect_sed_damage.py` | 101 | auto hook |
| P2 | `fix_file_hygiene.py` | 81 | auto hook，auto-fixer |
| P3 | 其餘 9 支 manual hooks | — | 低風險，可後續批次補 |

每支至少一個 positive test（`--ci` exit 0）+ 一個 negative test（故意觸發 error）。

**前置依賴**：HA-4（negative fixtures 目錄建立後可共用）。
**驗收**：auto hook 的 lint tools 全部有 `test_*.py` + 每支至少 2 tests。

### HA-8: CI ignore 文件化與 test-map 更新

CI 排除的 3 支 test files（`test_property.py`, `test_benchmark.py`, `test_pipeline_integration.py`）在本次審計已加 comment 說明原因。後續需在 `test-map.md` 中標記哪些 tests 是「CI 不跑」，避免維護者誤以為這些 tests 有 CI 保障。

**驗收**：`test-map.md` 有「CI 排除清單」段落，列出每支被 `--ignore` 的 test 及原因。

### HA-9: Coverage source 一致性 lint

本次審計修正了 `ci.yml` 中 `--cov=scripts/tools` 覆蓋 pyproject.toml source list 的問題。為防止再次發生，新增 CI step 或 pre-commit hook：解析 `ci.yml` 和 `pyproject.toml`，驗證 CI 的 `--cov` 參數不會覆蓋 pyproject.toml 的 `[tool.coverage.run] source`。

**方案選擇**：
- A（簡單）：CI 中 grep `--cov=` 出現次數 = 0（強制用 pyproject.toml）
- B（精確）：Python script 解析兩邊設定，比對一致性

**驗收**：CI 中不再出現 `--cov=<path>` 模式，或有 lint 自動阻擋。

---

## 候選 — Harness Audit v2.8.0 發想

> **來源**：v2.7.0-final release 收尾過程中累積的「糾錯成本高」系統性問題。每一項都對應一次實際發生的 2+ 小時 back-and-forth，不是假想情境。目標是「讓下一次 minor release 時，這些問題第一次出現就被 harness 攔下，而不是到 PR review 或 tag 前才發現」。
>
> **分類邏輯**：
> - **Flow** — 整個 session / release 流程層級（HA-10、HA-14、HA-15、HA-17）
> - **Lint** — 靜態檢查 / pre-commit 層級（HA-12、HA-13、HA-16）
> - **Testing Strategy** — 測試設計本身的根因修復（HA-11、HA-18）

### HA-10: Flake 自動重試 CI Policy（不是盲目全域 retry）

**問題來源**：v2.7.0 PR #26 CI 最終 20/20 綠，但 `TestWatchLoop_DebouncedReload_DetectsFileChange`（task #26）第一次跑 fail、第一次 rerun fail、第二次 rerun 33s 才通過。人工 `gh run rerun --failed` 重複三次，佔據 release 最後一小時。全域 retry 會掩蓋真正的新 bug，但完全不 retry 又會讓已知時間相依 test 拖住流程。

**設計**：`.github/workflows/ci.yml` 加 `flaky-tests.yaml` registry，格式：

```yaml
known_flakes:
  - test: "TestWatchLoop_DebouncedReload_DetectsFileChange"
    pattern: "watch_loop_debounced_reload_detects_file_change"
    max_retries: 2
    owner: "@exporter-team"
    tracked_by: "issue #NNN"   # 必須有對應 issue 追蹤根因修復進度
    expire_at: "v2.8.0"        # 過期後 CI 強制失敗，推動根因修復（避免白名單永久累積）
```

CI wrapper 解析該 registry → 只對 matching test 觸發 retry，其餘維持 fail-fast。test 若在 retry 後仍失敗，exit code 同 fail（不掩蓋真實 regression）。

**前置**：需要一個 `scripts/ops/ci_flake_retry.sh` 或改用 `gotestsum --rerun-fails=2 --rerun-fails-tests=<regex>`。

**驗收**：
1. `flaky-tests.yaml` 收錄 `TestWatchLoop_DebouncedReload_DetectsFileChange` + HA-11 的根因 PR 鏈接
2. v2.7.0 期間 rerun 手動次數的 TP99 from 3 → 0
3. expire_at 過期時 CI 失敗（驅動根因修復）

### HA-11: Fake-Clock 注入（根因修復 Go 時間相依測試）

**問題來源**：HA-10 是症狀治療，HA-11 才是根因。`TestWatchLoop_DebouncedReload_DetectsFileChange` 使用真實 `time.Sleep()` 等 300ms debounce 觸發，在 CI runner 負載浮動時容易 flake。類似問題會出現在未來每一個 `_debounce` / `_retry` / `_backoff` test。

**設計**：導入 `github.com/jonboulle/clockwork` 或 `github.com/benbjohnson/clock`（主流 fake-clock 套件）。將 `pkg/config/watch_loop.go` 的 `time.After(debounceInterval)` 改為接收 `Clock interface` 參數，生產用 real clock，test 用 fake clock 手動 advance。

**建議最小 patch**：

```go
type WatchLoop struct {
    clock clockwork.Clock   // 新欄位，default = clockwork.NewRealClock()
    // ...
}

func (wl *WatchLoop) debounceReload() {
    <-wl.clock.After(wl.debounceInterval)
    // ...
}
```

Test 改寫：

```go
fakeClock := clockwork.NewFakeClock()
wl := NewWatchLoop(WithClock(fakeClock))
// trigger change
fakeClock.Advance(300 * time.Millisecond)
// assert reload triggered
```

**前置**：
- `go get github.com/jonboulle/clockwork` 加入 `exporter/go.mod` 和 `tenant-api/go.mod`
- 盤點其他時間相依 test：`grep -rn "time.Sleep\|time.After" --include="*_test.go"` → 逐步遷移

**驗收**：
1. `TestWatchLoop_DebouncedReload_DetectsFileChange` 執行時間從 300ms+ → <10ms
2. 連續 100 次 `-count=100` run 零 flake
3. HA-10 的 `expire_at: v2.8.0` 能安全過期

### HA-12: ADR / 內部連結檔名一致性 Lint

**問題來源**：v2.7.0 doc-sync 過程，config-driven.md + .en.md 第一次寫入的連結是 `017-confd-hierarchy.md` + `018-defaults-inheritance-hot-reload.md`，實際檔名是 `017-conf-d-directory-hierarchy-mixed-mode.md` + `018-defaults-yaml-inheritance-dual-hash.md`。`doc-links-check` hook 只檢查 HTTP 連結，**內部相對路徑連結只要 target file 存在就 pass**，但**檔名改版本會漏抓**。

**設計**：新增 `scripts/tools/lint/check_internal_link_filenames.py`：

1. 解析所有 `.md` 檔案的內部連結（markdown 相對路徑格式）
2. 驗證 target file 是否存在（重複 `doc-links-check` 既有功能作為 baseline）
3. **新增**：若 target 位於 `docs/adr/` 或 `docs/design/` 下，檢查連結文字是否與檔名中的關鍵 token 匹配（例如連結文字含「conf-d-directory-hierarchy」但檔名是「confd-hierarchy」時報錯）

**挑戰**：匹配規則容易產生 false positive。建議先採「只對 ADR 目錄啟用，且要求連結 context 內提及 ADR 編號」的保守策略：

```python
# 若連結 context 提及 "ADR-017"，則 target 必須命中 017- 開頭的檔名
```

**前置**：無
**驗收**：把 017/018 連結故意改成錯誤檔名，lint 必須報錯。

### HA-13: Spoke 文件 Freshness Gate（防「空頭支票」）

**問題來源**：v2.7.0 doc audit 發現 `config-driven.md` front-matter 寫 `version: v2.7.0` 但**文件內沒有任何 v2.7.0 specific 內容**（conf.d/、_defaults.yaml、dual-hash、/effective 都沒提）。front-matter 是允諾，body 是空。`check_doc_freshness.py` 只檢查 version 字串，不檢查內容涵蓋度。

**設計**：新增 `scripts/tools/lint/check_spoke_doc_freshness.py`：

1. 讀取 `docs/internal/doc-map.md` 提供的版本 → 重點 features 對照表（新增 data file `docs/internal/version-feature-map.yaml`）：

```yaml
v2.7.0:
  must_mention_if_touched:
    - "config-driven.md": ["conf.d", "_defaults.yaml", "dual-hash", "/effective"]
    - "metrics-architecture.md": ["da_config_scan_duration_seconds", "da_config_reload_trigger_total"]
    - "architecture-and-design.md": ["ADR-017", "ADR-018", "Component Health"]
```

2. 對於 front-matter 標記 `version: v2.7.0` 的 spoke 文件，grep body 必須命中對照表的 N 個關鍵字
3. 命中數 < 門檻 → 報錯「front-matter 宣稱 v2.7.0 但 body 缺乏 v2.7.0 specific 內容」

**前置**：建立 `version-feature-map.yaml`（這是 ongoing 維護檔，每個 minor release 要 append 一節）。
**驗收**：
- v2.7.0 config-driven.md 修正前應該報錯，修正後 pass
- 故意把 config-driven.md body 的 v2.7.0 段落刪掉，lint 必須報錯

### HA-14: FUSE-side Git Write 防護 Wrapper

**問題來源**：CLAUDE.md §2b 規則「不要用 FUSE temp index 做 git commit」是因為 v2.7.0 過程踩過這個坑。目前靠「agent 記得規則」防守，但如果 agent 遺忘或新 session 沒讀 CLAUDE.md，這個陷阱會重現（FUSE 側 `git commit` 產出的 tree 不含修改，silent data loss）。

**設計**：兩層防守：

1. **Shell wrapper** — 在 `/sessions/compassionate-dreamy-hypatia/mnt/vibe-k8s-lab` 下新增 `.git/hooks/pre-commit` 加一行：

```bash
if [[ "$(pwd)" =~ ^/sessions/.*/mnt/ ]] || [[ "$(uname -a)" =~ Linux.*cowork ]]; then
    echo "❌ FUSE-side git commit detected. Use Windows escape hatch instead:" >&2
    echo "   cd C:\\Users\\vencs\\vibe-k8s-lab && git add ... && git commit --no-verify -F _msg.txt" >&2
    exit 1
fi
```

2. **Makefile target** — `make git-preflight` 擴充為偵測「當前是否在 FUSE 側」，若是則直接拒絕 `git commit` / `git add` 操作，並給出 Windows 側正確命令。

**挑戰**：某些 FUSE 操作（`git status` / `git log`）是安全的，只有 `git add` + `commit` + `push` 會踩雷。wrapper 必須白名單而非黑名單。

**前置**：無（都是 local hook + Makefile 修改）
**驗收**：在 FUSE 側打 `git commit` → pre-commit hook 立即擋下，輸出 Windows escape hatch 指令。

### HA-15: Session 起手式 PATH+PATHEXT Smoke Test

**問題來源**：v2.7.0 process 多次 PowerShell `cmd not recognized` / `.bat → git not found` 錯誤，根因是 Desktop Commander + cmd.exe + PowerShell 混用時 PATH + PATHEXT 傳遞不完整（詳見 windows-mcp-playbook §70 LL）。每個 session 起手時都可能重現。

**設計**：新增 `scripts/session-guards/path_smoke.sh`（Linux 側）+ `scripts/session-guards/path_smoke.ps1`（Windows 側），作為 `make session-start` 的第二步驟（繼 `vscode_git_toggle.py off` 之後）：

```bash
# path_smoke.sh — 驗證 cmd.exe + git.exe 可經由 Desktop Commander 呼叫
powershell -NoProfile -Command "
    \$env:PATHEXT = '.COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC;.PY;.PYW'
    \$env:PATH = 'C:\Windows\System32;C:\Windows;C:\Program Files\Git\cmd;C:\Program Files\Git\bin;' + \$env:PATH
    & 'C:\Windows\System32\cmd.exe' /c 'git --version' || exit 1
"
```

輸出：

```
[session-start] PATH smoke OK: cmd.exe + git.exe reachable via Desktop Commander
```

若 smoke fail 則立即給出修復指引（引用 windows-mcp-playbook §70 LL）。

**前置**：確認 `session_start` target 或對應 entrypoint 存在（CLAUDE.md 目前指的是 Agent 起手式，但沒有對應 Makefile target 聚合全部起手動作）。

**驗收**：新 session 首次執行 session-start 時，若 Windows 端缺失任一 git 元件 → smoke 立即報錯，不拖到 commit 時才發現。

### HA-16: CHANGELOG 計數一致性 Lint（tool count / JSX count / hook count）

**問題來源**：v2.7.0 release 尾端 task #48「Fix CHANGELOG line 50 Python tool count (112→117 / +5 vs 6 items / 共享 6)」——CHANGELOG 寫的工具總數和 README/CLAUDE.md 不一致，amend-push 一次才統一。這類「機械性可驗證的計數」重複漂移是永遠會發生的 bug。

**設計**：新增 `scripts/tools/lint/check_changelog_counts.py`：

1. 解析 CHANGELOG.md 每個 `## vX.Y.Z` section 中的 **所有數字**（regex: `(\d+)\s*個?(工具|hooks?|Rule Pack|JSX|ADR|tests?)`）
2. 對 **最新 released 版本** 的 section：
   - Python 工具總數 = `find scripts/tools -name "*.py" -not -name "_*"` 實際計數（扣除 `_lib_*.py` helpers）
   - JSX 工具總數 = `docs/assets/tool-registry.yaml` 實際條目
   - Pre-commit hook 總數 = `.pre-commit-config.yaml` 解析 auto + manual
   - ADR 總數 = `docs/adr/NNN-*.md` 檔案計數
3. 不一致則報錯，並給出「實際值 vs CHANGELOG 值」diff

**搭配**：README.md / CLAUDE.md 的相同計數也納入驗證。這其實是把 v2.7.0 新增的 `bump_docs.py --check` 擴展到 CHANGELOG。

**前置**：盤點 CHANGELOG 中「機械性可驗證計數」vs「敘述性計數」的分界（例如「新增 6 個場景文件」中的 6 就不該是全域計數，只是 release-local）。

**驗收**：故意把 CHANGELOG v2.7.0 的 `112 個 Python 工具` 改成 117 → lint 報錯；改回 112 → lint pass。

### HA-17: Desktop Commander 長命令 Watchdog Wrapper

**問題來源**：Desktop Commander MCP 有 60s 硬性 timeout，CI poll / Go test run / benchmark 經常跨越這個門檻。v2.7.0 多次手動改寫為「重導向到檔案 + cat」或拆成多個 <60s 命令（詳見 windows-mcp-playbook §核心原則）。每個 agent 都要重新學習這個技巧。

**設計**：新增 `scripts/ops/dc_watchdog.sh`（或 PowerShell 版）：

```bash
#!/bin/bash
# Usage: dc_watchdog.sh <logfile> <cmd...>
# 在背景跑 cmd，50s 後把 stdout tail 輸出，重導向到 logfile
LOGFILE="$1"; shift
"$@" > "$LOGFILE" 2>&1 &
PID=$!
sleep 50
if kill -0 "$PID" 2>/dev/null; then
    echo "[watchdog] cmd still running after 50s, streaming partial output:"
    tail -n 100 "$LOGFILE"
    echo "[watchdog] background PID=$PID, poll with: tail -f $LOGFILE"
    exit 2
else
    wait "$PID"
    tail -n 200 "$LOGFILE"
    exit $?
fi
```

agent 碰到「可能超過 60s」的命令時一律 `dc_watchdog.sh /tmp/log.txt <cmd>`，避免 timeout 截斷。

**前置**：確認 Desktop Commander 的 timeout 值（60s）不變，否則門檻要調。
**驗收**：一個 70s 的 `sleep 70 && echo done` 經 watchdog 包裝後不被 60s timeout 斬斷（partial output 可見）。

### HA-18: `engineering:testing-strategy` Skill 驅動的測試設計還債

**問題來源**：前述 HA-10 / HA-11 是「兩個 test 的局部修復」，但專案有 ~4232 tests，類似 flake 陷阱可能散佈全域。需要一次性用 `engineering:testing-strategy` skill 對測試策略做系統化還債。

**設計**：規劃一個 v2.8.0 Phase，動用 `engineering:testing-strategy` skill 輸出：

1. **時間相依測試清單** — `grep -rn "time.Sleep\|time.After\|time.NewTimer" --include="*_test.go"` 全量盤點，分類（必要 / 可替換為 fake clock / 可替換為 assert eventually）
2. **Test isolation audit** — 跑 `pytest-randomly --randomly-seed=0..10`，記錄 failure 模式
3. **Deadline / timeout conventions** — 為每個測試層級（unit / integration / E2E）訂定標準 deadline（unit <100ms、integration <5s、E2E <60s），超標的 test 必須加 `@pytest.mark.slow` marker
4. **Coverage 黑洞定位** — `pytest --cov` 逐 module 找出 <50% coverage 的 core logic，排優先級補 test

**輸出物**：`docs/internal/testing-strategy-v2.8.0.md`，作為 v2.8.0 Phase A 的 spec。

**前置**：Invoke `engineering:testing-strategy` skill（已在 plugin 中啟用）→ 帶入現有 test 結構 + flake 清單 + coverage 報告作為 context。

**驗收**：spec 文件產出後，每一項都有對應的 HA-* 候選項（或直接進 CHANGELOG）。v2.8.0 關版時 flake 數、test-isolation violation、coverage < 50% 的 module 數量比 v2.7.0 降低 50% 以上。

---

## 候選 — 架構與工程實踐

### _lib_python.py 共用函式庫擴充

v2.3.0 完成四子模組拆分。下一步將散落在多個工具中的重複 pattern 收進子模組：common argparse setup（`--prometheus-url`, `--config-dir` 等標準參數）、ConfigMap patch helper。

### Python 大型工具結構瘦身（持續改善）

**v2.6.0 已完成**：`generate_alertmanager_routes.py`（21 helpers extracted）+ `init_project.py`（6 helpers extracted）。

**v2.7.0 進度**：未推進，且 `scaffold_tenant.py` 於 v2.7.0 新增 `--topology=1:N`、`--mapping-instance`、`--mapping-filter`、`--routing-profile` 參數後反而略為增長；`validate_docs_versions.py` 同理（1,032 → 1,109 行，因加入歷史版本 skip 規則）。**重新確認：大型工具的「每次小改」會讓瘦身壓力持續累積，必須排入 minor release 的明確 phase 才會發生。**

**殘留**：3 支 1,000+ 行工具待重構：

| 工具 | 行數（v2.7.0） | 建議方向 | 時機 |
|------|------|---------|------|
| `scaffold_tenant.py` | 1,153 | template generation 與 file I/O 分離；topology/mapping/routing-profile 三個子命令應考慮拆為獨立 CLI | v2.8.0 |
| `onboard_platform.py` | 1,131 | 與 `init_project.py` 有功能重疊，評估合併 | v2.8.0 |
| `validate_docs_versions.py` | 1,109 | 拆出 version pattern registry 為獨立 YAML data file；historical planning skip 邏輯獨立成 helper module | v2.8.0 |

### 錯誤訊息衛生化 Policy 文件化

v2.6.0 Lesson Learned：所有外部 API client 的 error response 處理應有統一 policy。撰寫 `docs/internal/error-handling-policy.md`，定義「log full body, return status code only」原則，供未來新增 provider 遵循。

---

## 候選 — v2.6.0 開發過程新發現

### 雙語 SSOT 架構切換（EN-first）

v2.5.0 Phase D 完成影響評估文件（`docs/internal/ssot-language-evaluation.md`）。**v2.7.0 進度**：Phase 1 工具準備完成（`migrate_ssot_language.py` 支援 `--dry-run` / `--execute --git`，lint hooks 支援 `.en.md` + `.zh.md` dual-mode auto-detect，pilot report 見 `docs/internal/ssot-migration-pilot-report.md`）。**v2.8.0 執行 Phase 2**：66 對檔案 + mkdocs.yml 原子性 commit，配合 minor release 的 migration window。

### GitLab Tracker Rate Limit 防護

v2.6.0 Phase .e 實作 GitLab MR 支援，但 tracker sync 失敗時無 exponential backoff。自託管 GitLab 的 Rate Limit 可能較嚴。短期新增 `TA_TRACKER_SYNC_INTERVAL` 環境變數；中期實作 exponential backoff with jitter。

### CI/CD Pipeline 狀態透傳

PR/MR 建立後 CI 驗證結果未回傳 UI。Domain Expert 無法在 Portal 內得知 PR 的 CI Status Check 狀態。未來 UI 應顯示「等待合併（CI 驗證失敗）」等狀態。需 GitHub Check Runs API / GitLab Pipeline API 整合。

### tenant-manager.jsx Design Token 全面遷移

v2.6.0 Phase .a0 消除了 hardcoded hex colors，但 font-size 和 spacing 仍有 27+ 處 hardcoded px 值。需全面遷移至 `var(--da-*)` tokens。

### Slider/Input 控件 Token 標準化

roi-calculator、migration-roi-calculator、cost-estimator 的 slider thumb/track 使用 hardcoded 6px/18px。需新增表單控件專用 tokens（`--da-form-slider-track-height`、`--da-form-slider-thumb-size`）。

### GitLab Token 安全建議強化

v2.6.0-final 外部審查指出：GitLab `api` scope token 等同專案最高權限。ADR-011 應新增「強烈建議綁定 Bot Account / Project Access Token，不使用個人 PAT」的安全提醒。

### WritePR git push 失敗處理

v2.6.0 Phase .c-R2 發現 WritePR git push 失敗後仍回傳 PRWriteResult。應在 push 失敗時 rollback 並回傳 error，而非靜默成功。

---

## 候選 — CI/CD 改善

### Release automation

五線版號（platform/exporter/tools/portal/tenant-api）的 tag + GitHub Release 仍是手動流程。可用 GitHub Actions 監聽 tag push 自動觸發 Release Notes 產生（基於 CHANGELOG section）和 OCI image build/push。v2.3.0 CI matrix 已到位，為自動化提供基礎。

### dependabot / renovate

管理 Python / Go / npm 依賴更新，確保安全修補及時套用。CI matrix 穩定後優先導入。

### CI caching 策略

pip cache、Go module cache、pre-commit environment cache。減少 CI 重複安裝時間，預計 CI 執行時間降低 30-50%。

---

## 候選 — Go threshold-exporter 效能

> v2.3.0 已完成：Config Info Metric、WatchLoop integration test、resolveConfigPath test、Fail-Safe Reload E2E、detectConfigSource 四情測試。以下為後續可探索的方向。

### 並行化 scanDirFileHashes

v2.1.0 嘗試 goroutine pool（8 workers）但因 goroutine 建立/channel 同步開銷在 tmpfs/overlayfs 上反而變慢而回退。未來若部署在高延遲儲存（NFS/EBS）上可重新評估。需要 benchmark 在真實 disk 上驗證 break-even point。

### YAML 解析並行化

`fullDirLoad` 遍歷 1000 個 YAML 檔做 `yaml.Unmarshal` 是 CPU-bound。可用 goroutine pool 並行解析（每個檔案獨立），預期在 multi-core CPU 上 FullDirLoad 加速 2-4×。需注意 `applyBoundaryRules` 寫入共享 map 的 race。

### Persistent merged config（增量 patch 擴展至 _defaults）

v2.1.0 的 incremental merge 只處理 tenant 檔變動。當 `_defaults.yaml` 或 `_profiles.yaml` 變動時仍走 full merge。可維護 persistent merged config + dependency graph，實現全路徑 incremental merge。複雜度較高，ROI 需評估（defaults 變動頻率遠低於 tenant）。
