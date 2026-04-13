---
title: DX Tooling Backlog
tags: [dx, tooling, backlog, internal]
audience: [maintainers, contributors]
lang: zh
version: v2.6.0
---

# DX Tooling Backlog

> Forward-looking DX 改善追蹤。已完成的項目見 [CHANGELOG.md](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CHANGELOG.md)。
> 與產品 Roadmap（`architecture-and-design.md` §5）分開管理：§5 放核心功能方向，這裡放 DX / 內部工具 / 測試品質。

## 狀態說明

- **候選** — 已識別但尚未排入
- **進行中** — 當前 iteration 正在實作

---

## 候選 — DX 工具改善

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

---

## 候選 — 測試品質

### Snapshot test 擴展

為 `generate_report()` / `generate_markdown()` / `print_text_report()` 輸出加入 JSON snapshot 穩定性測試，防止格式意外變更。v2.3.0 已建立 snapshot 基礎設施，可擴展覆蓋範圍。

### test docstring 覆蓋率 lint

新增 pre-commit hook 或 lint script，自動檢查所有 test method 是否有繁體中文 docstring。確保測試意圖一目了然。

### Property-based testing 進一步擴展

v2.6.0 已新增 22 個 Hypothesis tests（RFC 1123, SHA-256, drift, YAML round-trip, kustomization）。進一步擴展方向：將核心 property tests（如 `parseMetricKey` 的 idempotency、`clampDuration` 的 monotonicity）加入。考慮 Go 端的 rapid/gopter property testing。

### Go test `-race` CI 預設化

v2.6.0 Phase .e 發現 3 個 data race（直到 `-race` flag 才偵測到）。應將 `go test -race` 設為 CI 預設，而非 optional flag。

---

## 候選 — Harness Audit v2.7.0 發想

> 來源：Harness Audit v2.7（local-only，不進 repo）第一輪 + 第二輪 review。
> 這些項目聚焦「防守工具本身的品質和可信度」，與上方測試品質的差異在於：上方是測產品程式碼的測試，這裡是測「測試工具」的測試。

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

## 候選 — 架構與工程實踐

### _lib_python.py 共用函式庫擴充

v2.3.0 完成四子模組拆分。下一步將散落在多個工具中的重複 pattern 收進子模組：common argparse setup（`--prometheus-url`, `--config-dir` 等標準參數）、ConfigMap patch helper。

### Python 大型工具結構瘦身（持續改善）

**v2.6.0 已完成**：`generate_alertmanager_routes.py`（21 helpers extracted）+ `init_project.py`（6 helpers extracted）。

**殘留**：3 支 1,000+ 行工具待重構：

| 工具 | 行數 | 建議方向 | 時機 |
|------|------|---------|------|
| `scaffold_tenant.py` | 1,153 | template generation 與 file I/O 分離 | v2.7.0 |
| `onboard_platform.py` | 1,131 | 與 `init_project.py` 有功能重疊，評估合併 | v2.7.0 |
| `validate_docs_versions.py` | 1,032 | 拆出 version pattern registry 為獨立 data file | v2.7.0 |

### 錯誤訊息衛生化 Policy 文件化

v2.6.0 Lesson Learned：所有外部 API client 的 error response 處理應有統一 policy。撰寫 `docs/internal/error-handling-policy.md`，定義「log full body, return status code only」原則，供未來新增 provider 遵循。

---

## 候選 — v2.6.0 開發過程新發現

### 雙語 SSOT 架構切換（EN-first）

v2.5.0 Phase D 完成影響評估文件（`docs/internal/ssot-language-evaluation.md`）。建議 v2.7.0 實施 EN-first SSOT 遷移（123 markdown + 32 JSX + 15 Rule Pack + 7 lint hook 影響）。

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
