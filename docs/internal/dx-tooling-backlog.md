---
title: DX Tooling Backlog
lang: zh
version: v2.3.0
---

# DX Tooling Backlog

> Forward-looking DX 改善追蹤。已完成的項目見 [CHANGELOG.md](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CHANGELOG.md)。
> 與產品 Roadmap（`architecture-and-design.md` §5）分開管理：§5 放核心功能方向，這裡放 DX / 內部工具 / 測試品質。

## 狀態說明

- **候選** — 已識別但尚未排入
- **進行中** — 當前 iteration 正在實作

## 歷史完成摘要

已完成項目詳見 CHANGELOG.md。

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

84 個 Python 工具的 argparse help text 格式標準化：統一使用動詞開頭、統一中英文語言（依 `detect_cli_lang()`）、統一 metavar 命名風格。

---

## 候選 — 互動工具

### Cost Estimator (JSX)

從 Capacity Planner 延伸，輸入雲端供應商（AWS/GCP/Azure）和 instance type，估算每月 Prometheus + 儲存成本。

### Release Notes Generator (JSX)

從兩個版本號的 CHANGELOG 段落自動產生面向不同受眾（Platform/Tenant/DBA）的精簡 release notes。

### Threshold Heatmap (JSX)

視覺化多 tenant 的閾值分佈（P50/P95/P99），基於 `threshold-recommend --json` 輸出，快速發現 outlier。

### Config Diff Visualizer (JSX) — 完整版

v2.2.0 在 Routing Trace Tab 加入 Copy JSON 匯出。完整版需要獨立工具：side-by-side 比較兩個 tenant 或兩個版本的 YAML 配置差異，利用 `config-diff --json` 輸出渲染 diff view（新增/移除/修改 highlight）。

### Notification Template Editor (JSX)

v2.2.0 完成了 Previewer（7 receiver 預覽）。下一步擴展為 Template Editor：自定義通知內容格式、變數引用、條件區塊，並匯出為 Alertmanager template 片段。

### Operator Setup Wizard (JSX)

互動式引導用戶配置 Prometheus Operator 整合：偵測 Operator 版本、選擇 CRD 類型、產生 `operator-generate` CLI 命令、預覽 CRD YAML。與 v2.3.0 的 Operator 指南搭配。

### Interactive Tool Playwright E2E

v2.2.0 的 `guided-flow-e2e` hook 驗證了 flows.json 結構完整性。進一步用 Playwright 建立真實瀏覽器 smoke test：確認 30 支 JSX 工具可載入、基本互動正常、無 console error。需 CI 環境支持瀏覽器。

---

## 候選 — 測試品質

### Snapshot test 擴展

為 `generate_report()` / `generate_markdown()` / `print_text_report()` 輸出加入 JSON snapshot 穩定性測試，防止格式意外變更。v2.3.0 已建立 snapshot 基礎設施，可擴展覆蓋範圍。

### test docstring 覆蓋率 lint

新增 pre-commit hook 或 lint script，自動檢查所有 test method 是否有繁體中文 docstring。確保測試意圖一目了然。

### Property-based testing 擴展

目前 `test_property.py` 使用 Hypothesis 做 YAML round-trip 測試，但需要 `hypothesis` library 才能跑。考慮將核心 property tests（如 `parseMetricKey` 的 idempotency、`clampDuration` 的 monotonicity）移入不需額外依賴的 parametrize 測試中。

---

## 候選 — 架構與工程實踐

### _lib_python.py 共用函式庫擴充

v2.3.0 完成四子模組拆分。下一步將散落在多個工具中的重複 pattern 收進子模組：common argparse setup（`--prometheus-url`, `--config-dir` 等標準參數）、ConfigMap patch helper。

### stderr 路由全面掃描

v2.3.0 新增 SAST Rule 7。持續監控新增工具是否遵守 stderr 路由規範，必要時擴展掃描覆蓋範圍。

### Full CRUD API Server (Tenant Manager backend)

v2.3.0 Tenant Manager 為純前端（讀 platform-data.json + 產生 YAML）。下一步引入輕量 REST API（Go 或 Python FastAPI）：CRUD tenant 配置、即時驗證、WebSocket 推播配置變更事件。此為重大架構變更，需獨立 ADR 評估。

### threshold-exporter 演化為 K8s Operator

監聽自定義 `DynamicAlertTenant` CRD，取代 ConfigMap + Directory Scanner 模式。需 Operator SDK、RBAC 設計、CRD versioning。v2.3.0 的 `detectConfigSource()` 三態偵測已為此方向奠基。規模大，建議分階段評估。

---

## 候選 — v2.4.0 自動化防守（v2.3.0 Release Lessons Learned）

> v2.3.0 release 過程中手動發現的 6 類問題。每一項都可以用 pre-commit hook 或 CI check 自動防守，避免下次 release 再踩同樣的坑。

### 1. 版號同步 lint（image tag + Helm version 全域掃描）

**問題**：42 個檔案中的 Docker image tag（`da-tools:v2.1.0`）、Helm `--version` flag、`Chart.yaml`、`VERSION` 檔案、`mkdocs.yml` extra vars 全部手動更新，遺漏率極高。

**方案**：新增 `check_version_sync.py` pre-commit hook。從 `components/*/app/VERSION` 和 `Chart.yaml` 讀取 source-of-truth 版號，掃描 `docs/`、`components/*/README.md`、`k8s/`、`.github/workflows/`、`mkdocs.yml` 中所有 `ghcr.io/vencil/<image>:v*` 和 `--version *` pattern，確保與 source-of-truth 一致。

**優先序**：P0 — 這是 v2.3.0 最大的手動工作量來源

### 2. build.sh ↔ COMMAND_MAP 雙向同步檢查

**問題**：`entrypoint.py` 新增 `opa-evaluate` 指向 `policy_opa_bridge.py`，但 `build.sh` TOOL_FILES 漏包該檔案，導致 Docker image 中 `da-tools opa-evaluate` 會 crash。

**方案**：擴展既有 `test_entrypoint.py::TestCommandMapConsistency::test_command_map_covers_build_tools`，增加反向檢查：COMMAND_MAP 中每個 `.py` value 也必須存在於 `build.sh` TOOL_FILES 中。或新增獨立的 `check_build_completeness.py` hook。

**優先序**：P0 — 功能損壞（使用者看得到的 crash）

### 3. ZH/EN 文件結構同步 lint

**問題**：`cli-reference.en.md` 缺少整個 "Operator + Federation" 章節和 `opa-evaluate` 指令，而 ZH 版本有。現有的 `check_doc_links.py` 只檢查檔案存在性，`validate_docs_versions.py` 只檢查 frontmatter 版號和計數，兩者都抓不到章節級內容漂移。

**方案**：新增 `check_bilingual_structure.py`——對每組 `*.md` / `*.en.md` pair 提取 `##` / `###` / `####` 標題，比對章節骨架是否一致。允許翻譯差異但 section count 和 heading hierarchy 必須匹配。

**優先序**：P1 — 非 crash 但用戶體驗差（EN 用戶缺少文件）

### 4. entrypoint help text ↔ COMMAND_MAP 全覆蓋 lint

**問題**：`COMMAND_MAP` 中已註冊 `opa-evaluate`，但 `_build_help_text()` 中英雙語 help text 忘記列入該指令，導致 `da-tools --help` 不顯示。

**方案**：擴展 `test_entrypoint.py::TestHelpTextConsistency::test_all_commands_in_english_help`，將 `_HELP_EXEMPT` 白名單縮小到真正豁免的項目。或新增 `check_help_completeness` hook，直接比較 `COMMAND_MAP.keys()` 與 help text 中出現的 command 名稱。

**優先序**：P1 — 已有部分測試但白名單太寬鬆

### 5. cheat-sheet ↔ cli-reference ↔ COMMAND_MAP 三向一致性

**問題**：`operator-check`、`operator-generate`、`rule-pack-split`、`opa-evaluate` 四個指令在不同文件中覆蓋不一致——有的只在 ZH 版有、有的 cheat sheet 漏列。

**方案**：`check_cli_coverage.py` 已做三向比對，但目前只在 `test_full_coverage_check` 中跑。升級為 pre-commit hook（auto stage），每次 commit 自動執行，提前攔截不一致。

**優先序**：P1 — 工具已存在，只需接入 hook

### 6. JSX 工具 i18n 完整性 lint

**問題**：jsx-loader.html 的語言切換按鈕兩個分支回傳相同字串（`'中文 / EN'`），且 `tenant-manager` 漏入 CUSTOM_FLOW_MAP。

**方案**：新增 `check_jsx_i18n.py`，掃描 `jsx-loader.html` 確認：(a) TOOL_META 和 CUSTOM_FLOW_MAP 的 key set 一致，(b) `window.__t` 呼叫的兩參數不相同（防止 copy-paste bug），(c) 語言切換函式的兩分支回傳不同值。

**優先序**：P2 — 視覺 bug，非功能損壞

### 7. README 雙語導航對稱 lint

**問題**：`README.md`（ZH）缺少 `[English](README.en.md)` 連結，而 `README.en.md` 有 `[中文](README.md)` 連結，導致 ZH 讀者無法切換到 EN。

**方案**：在 `check_bilingual_structure.py`（第 3 項）中一併檢查：每組 `*.md` / `*.en.md` pair 的前 20 行都必須包含指向對方的連結。

**優先序**：P2 — 與第 3 項合併實作

### 8. Makefile target 與 dx 工具聯動檢查

**問題**：新增 `generate_tenant_metadata.py` 但 Makefile `platform-data` target 沒有呼叫它，導致 Tenant Manager UI 的 config drift 偵測失效。

**方案**：新增 `check_makefile_targets.py`——從 `scripts/tools/dx/` 中所有 `generate_*.py` 和 `sync_*.py` 提取工具清單，驗證每個 DX 生成工具都被至少一個 Makefile target 引用。防止新增工具但忘記接入 Makefile 的情況。

**優先序**：P2 — 功能缺失但不 crash

---

## 候選 — CI/CD 改善

### Release automation

四線版號（platform/exporter/tools/portal）的 tag + GitHub Release 仍是手動流程。可用 GitHub Actions 監聽 tag push 自動觸發 Release Notes 產生（基於 CHANGELOG section）和 OCI image build/push。v2.3.0 CI matrix 已到位，為自動化提供基礎。

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
