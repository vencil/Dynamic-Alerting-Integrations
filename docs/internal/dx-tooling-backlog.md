---
title: DX Tooling Backlog
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
