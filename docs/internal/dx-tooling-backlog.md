---
title: DX Tooling Backlog
lang: zh
version: v2.1.0
---

# DX Tooling Backlog

> Forward-looking DX 改善追蹤。已完成的項目見 [CHANGELOG.md](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CHANGELOG.md)。
> 與產品 Roadmap（`architecture-and-design.md` §5）分開管理：§5 放核心功能方向，這裡放 DX / 內部工具 / 測試品質。

## 狀態說明

- **候選** — 已識別但尚未排入
- **進行中** — 當前 iteration 正在實作

## v2.1.0 已完成摘要

以下項目在 v2.1.0 中完成，從 backlog 移出：

- `query_prometheus_instant` 共用化 — 5 支 ops 工具（diagnose, byo_check, federation_check, validate_migration, baseline_discovery）統一遷移至 `_lib_python.py`，消除約 80 行重複程式碼
- DX 工具測試覆蓋 — 新增 4 個測試檔（generate_changelog, suggest_related, generate_tool_map, add_frontmatter），共 63 tests
- Lint 工具測試覆蓋 — 新增 3 個測試檔（check_translation, validate_mermaid, fix_doc_links），共 52 tests
- Pre-commit 效能優化 — `validate_docs_versions.py` 引入三層快取（file/content/rglob），15.8s → 6.6s（降 58%）
- Self-Service Portal ADR-007 支援 — routing profiles + domain policy 驗證 + 範例切換
- 測試總數：3070 collected（3027 passed, 43 skipped）

---

## 候選 — DX 工具改善

### pre-commit 剩餘瓶頸優化

v2.1.0 優化了最慢的 `validate_docs_versions.py`（15.8→6.6s）。另兩個瓶頸 `check_bilingual_annotations`（~5s）和 `lint_tool_consistency`（~4s）也可套用相同的 rglob/content 快取策略。目標：pre-commit total sequential time < 20s。

### check_doc_freshness.py Helm chart 版號檢查

目前只檢查 Docker image 版號，擴展到 `helm install/upgrade` 命令中的 `--version` flag 比對。

### check_doc_links.py 跨語言連結驗證

驗證 zh doc 引用的 en doc 確實存在（反之亦然），目前只做同語言內部連結。pre-commit hook `doc-links-check` 現有 47 個 broken links 待修復（主要是 README.md 的相對路徑）。

### check_cli_coverage.py 整合 pre-commit

加入 `.pre-commit-config.yaml` auto-stage hooks，攔截 entrypoint.py 修改但文件未同步更新。

### validate_all.py `--fix --diff` combo

`--fix` 後自動顯示 diff summary（目前 `--fix` 和 `--diff-report` 是獨立的），減少手動操作步驟。

### validate_all.py fail_under 提升

目前 fail_under = 64。測試已達 3070，基線可安全提升至 68-70。

### pre-commit 剩餘失敗修復

3 個 auto-run hook 仍有 pre-existing failures：version-consistency（4 個 frontmatter version drift）、tool-consistency-check（3 個新 JSX tools 未加入 Hub/TOOL_META）、doc-links-check（47 個 broken links）。逐步修復以達成全綠。

### generate_doc_map.py ADR 預設包含

目前需要 `--include-adr` 才會包含 ADR 文件。考慮改為預設行為（或 `--exclude-adr` 反向控制），減少遺漏風險。pre-commit hook 已加 `--include-adr`，但 CLI 預設值不一致。

### Metric Dictionary 自動驗證

`metric-dictionary.yaml` 的 metric 名稱應與 Rule Pack YAML 中實際使用的 recording rule 交叉驗證。偵測字典中存在但 Rule Pack 不使用的 stale entry，和 Rule Pack 使用但字典未收錄的 undocumented metric。

---

## 候選 — 互動工具

### Cost Estimator (JSX)

從 Capacity Planner 延伸，輸入雲端供應商（AWS/GCP/Azure）和 instance type，估算每月 Prometheus + 儲存成本。

### Release Notes Generator (JSX)

從兩個版本號的 CHANGELOG 段落自動產生面向不同受眾（Platform/Tenant/DBA）的精簡 release notes。

### Notification Template Previewer (JSX)

在瀏覽器中預覽 Slack/Teams/PagerDuty 通知 payload 的渲染效果，搭配 `test-notification --dry-run --json` 輸出。同步列在 `architecture-and-design.md` §5.7。

### Threshold Heatmap (JSX)

視覺化多 tenant 的閾值分佈（P50/P95/P99），基於 `threshold-recommend --json` 輸出，快速發現 outlier。

### Config Diff Visualizer (JSX)

Side-by-side 比較兩個 tenant 或兩個版本的 YAML 配置差異。利用 `config-diff --json` 輸出，在瀏覽器中渲染 diff view（新增/移除/修改 highlight）。

### Interactive Tool E2E Smoke Test

26 支 JSX 工具目前無自動化驗證。用 Puppeteer 或 Playwright 建立 smoke test：確認每支工具可載入、基本互動運作正常、無 console error。可整合至 `make lint-docs` 或獨立 CI step。

---

## 候選 — 測試品質

### Python 工具整合測試 (Pipeline-level)

目前 73 支 Python 工具多數只有 unit test。新增 integration-level test 串接多工具流程，例如 `scaffold → validate → diagnose` 完整 pipeline mock，驗證工具間的資料格式相容性。

### Snapshot test 擴展

為 `generate_report()` / `generate_markdown()` / `print_text_report()` 輸出加入 JSON snapshot 穩定性測試，防止格式意外變更。

### test docstring 覆蓋率 lint

新增 pre-commit hook 或 lint script，自動檢查所有 test method 是否有繁體中文 docstring。確保測試意圖一目了然。

### conftest fixture 精簡

`routing_dir` fixture 僅 3 個檔案使用，評估是否值得內聯化。`config_dir` fixture 維持現狀（137+ 使用處）。v2.1.0 已移除 4 個未使用的 session-scoped fixtures。

### Property-based testing 擴展

目前 `test_property.py` 使用 Hypothesis 做 YAML round-trip 測試，但需要 `hypothesis` library 才能跑。考慮將核心 property tests（如 `parseMetricKey` 的 idempotency、`clampDuration` 的 monotonicity）移入不需額外依賴的 parametrize 測試中。

### Go test coverage 持續追蹤

v2.1.0 新增 `parsePromDuration`/`isDisabled`/`clampDuration` 和 collector 測試。仍有未覆蓋的函數：`WatchLoop`（goroutine lifecycle）、`resolveConfigPath`（env var + auto-detect）。`WatchLoop` 可用 short interval + stop channel 做 integration test。

---

## 候選 — 架構與工程實踐

### _lib_python.py 共用函式庫擴充

v2.1.0 完成 `query_prometheus_instant` 統一化。下一步將散落在多個工具中的重複 pattern 收進共用函式庫：YAML 載入 + schema validation helper、ConfigMap patch helper、common argparse setup（`--prometheus-url`, `--config-dir` 等標準參數）。

### _lib_python.py 模組拆分

隨著工具數量增長至 73 支，`_lib_python.py` 職責過廣（GUARDRAILS、RECEIVER_TYPES、write_text_secure、YAML helpers、HTTP helpers、Prometheus query）。考慮拆分為 `_lib_constants.py`（純常數）+ `_lib_io.py`（file I/O helpers）+ `_lib_validation.py`（schema 驗證）+ `_lib_prometheus.py`（query 相關），降低模組耦合度。

### stderr 路由全面掃描

v2.1.0 修正了 4 處 error print 到 stdout 的問題。建議新增 SAST Rule 7：AST 掃描所有 `print("ERROR` / `print("Error` / `print(f"ERROR` 呼叫，確保帶有 `file=sys.stderr`。可整合至 `test_sast.py`。

### Python typing 覆蓋

核心模組 `_lib_python.py` 和高頻工具（`scaffold_tenant.py`, `generate_alertmanager_routes.py`, `validate_config.py`）加入 type hints。搭配 `mypy --strict` 或 `pyright` 漸進式啟用。

### CLI help 一致性 lint

73 個 Python 工具的 argparse help text 格式標準化：統一使用動詞開頭、統一中英文語言（依 `detect_cli_lang()`）、統一 metavar 命名風格。

### Tool exit code contract test

目前 exit code 規範（0=success, 1=runtime error, 2=argparse only）靠 convention 維護。新增 contract test：對每個工具執行 `--help`（期望 exit 0）和 invalid args（期望 exit 2），驗證 argparse 行為一致。

---

## 候選 — CI/CD 改善

### GitHub Actions CI matrix

為 Python tests 和 Go tests 建立 GitHub Actions workflow，包含 matrix（Python 3.10/3.12, Go 1.22/1.23）。pre-commit hooks 作為 CI first gate。3070 tests 在 CI 環境預估約 90s。

### Container image size audit

`threshold-exporter` 和 `da-tools` image 做 multi-stage build 瘦身評估。`da-portal` 的 nginx alpine image 已 pin 到 patch version，但可進一步用 distroless base。

### Release automation

四線版號（platform/exporter/tools/portal）的 tag + GitHub Release 仍是手動流程。可用 GitHub Actions 監聯 tag push 自動觸發 Release Notes 產生（基於 CHANGELOG section）和 OCI image build/push。

---

## 候選 — Go threshold-exporter 效能

> v2.1.0 已完成：logConfigStats 取代 Resolve()、mtime guard、incremental merge、DirEntry.Info()、byte cache。以下為後續可探索的方向。

### 並行化 scanDirFileHashes

v2.1.0 嘗試 goroutine pool（8 workers）但因 goroutine 建立/channel 同步開銷在 tmpfs/overlayfs 上反而變慢而回退。未來若部署在高延遲儲存（NFS/EBS）上可重新評估。需要 benchmark 在真實 disk 上驗證 break-even point。

### YAML 解析並行化

`fullDirLoad` 遍歷 1000 個 YAML 檔做 `yaml.Unmarshal` 是 CPU-bound。可用 goroutine pool 並行解析（每個檔案獨立），預期在 multi-core CPU 上 FullDirLoad 加速 2-4×。需注意 `applyBoundaryRules` 寫入共享 map 的 race。

### Persistent merged config（增量 patch 擴展至 _defaults）

v2.1.0 的 incremental merge 只處理 tenant 檔變動。當 `_defaults.yaml` 或 `_profiles.yaml` 變動時仍走 full merge。可維護 persistent merged config + dependency graph（哪個 default key 被哪些 tenant 使用），實現全路徑 incremental merge。複雜度較高，ROI 需評估（defaults 變動頻率遠低於 tenant）。

### Composite hash 改用 per-file hash XOR

目前 composite hash 是 SHA-256(concat(per_file_hashes))。若改用 XOR-fold 或 sorted hash list comparison，可在 mtime guard 路徑省去 compositeHasher 的 Write 成本。但當前成本 < 0.1ms，ROI 低。

### Go test coverage 補完

`WatchLoop`（goroutine lifecycle + mtime guard 整合）、`resolveConfigPath`（env var + auto-detect）仍未有 unit test。可用 short interval + stop channel + temp dir 做 integration test，驗證 mtime guard 在真實 watch 循環中的行為。
