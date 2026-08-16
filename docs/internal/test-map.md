---
title: "測試架構導覽 (Test Map)"
tags: [testing, navigation, internal]
audience: [maintainers, ai-agent]
version: v2.9.0
lang: zh
---

# 測試架構導覽 (Test Map)

> 測試基礎設施結構與慣例速查，供 AI Agent 與開發者快速掌握測試配置。
>
> **相關文件：** [Testing Playbook](testing-playbook.md)（排錯手冊）· [Benchmark Playbook](benchmark-playbook.md)（方法論、踩坑）· [進階場景與測試覆蓋](test-coverage-matrix.md)（E2E + 功能域矩陣）· [Benchmarks](../benchmarks.md)（效能數據）

## 目錄結構

```
tests/
├── conftest.py          # 全域 sys.path + pytest fixtures
├── factories.py         # 共用 factory helpers + PipelineBuilder
├── ops/                 # scripts/tools/ops 對應測試（55 檔）
├── dx/                  # scripts/tools/dx 對應測試（8 檔）
├── lint/                # scripts/tools/lint 對應測試（20 檔）
├── shared/              # 跨類別 / 基礎設施測試（15 檔）
├── e2e/                 # Playwright E2E 測試
├── fixtures/            # 共用測試資料
├── snapshots/           # 快照基線（JSON / snap）
├── scenarios/           # Shell 場景腳本
└── alertmanager-inhibit/ # 手寫 AM config 的抑制語意 gate（獨立 Go module）
```

`alertmanager-inhibit/` 是唯一的 Go 測試 module（自帶 `go.mod`，把 alertmanager 依賴隔離在兩個 production module 之外）。它用 Alertmanager 自己的 matcher 實作評估 `try-local/alertmanager.yml` 與 `k8s/03-monitoring/configmap-alertmanager.yaml` 的**實際抑制行為**——`amtool check-config` 只驗語法，對 #1132 那種「語法合法但語意錯、且靜默吃掉通知」的規則全綠。入口 `make test-am-inhibit`；CI 走 `Go Tests (1.26)` 聚合閘。新增 sentinel／inhibit 規則時（ADR-003 Sentinel+Inhibit paradigm）應同步補一組斷言。

v2.7.0 將 98 個 `test_*.py` 從 `tests/` 根目錄搬入 `ops/` / `dx/` / `lint/` / `shared/` 四個子目錄，與 `scripts/tools/` 的分類對齊。`conftest.py` 和 `factories.py` 留在根目錄，pytest 自動遞迴收集子目錄測試。

## 測試注入 Seam (v2.8.0 後標準)

> **適用範圍：** `components/threshold-exporter/app/*_test.go`（Go package main）。
> v2.8.0 一連串 PR (#363–#369) 把三個 global-swap antipatterns 從測試中拆掉，
> 改走 `ConfigManager` 上 mirror 自 `SetClock` 的 test-only setter 注入。寫新測試前**先用對 seam，不要再引入 global swap**；老的 helper 已經刪除。

### 速查決策表

| 測試要驗 | 用 | 不要再用 |
|---|---|---|
| metrics 寫入 | `fresh, _ := freshMetrics(t)` + `m.SetMetrics(fresh)` | ~~`withIsolatedMetrics(t)`~~（PR #365 移除） |
| log 輸出 | `log.New(&buf, "", 0)` + `m.SetLogger(testLogger)` | ~~`log.SetOutput(&buf)`~~（PR #368 移除） |
| WatchLoop / 計時 | `startWatchLoopWithFakeClock(t, m, interval)` + `Advance` + `waitFor(state)` | ~~`time.Sleep` 等 ticker / debounce~~（PR #369 移除 WatchLoop tests） |
| Scanner 直接呼叫 | `scanDirHierarchicalWithMetrics(dir, nil, fresh, nil)` | (legacy `scanDirHierarchical(dir, nil)` 仍 OK) |

### 完整 patterns

**Metrics injection** — assert 計數 / histogram bucket：

```go
fresh, _ := freshMetrics(t)
m := NewConfigManager(dir)
m.SetMetrics(fresh)
m.Load()
// assert via fresh.parseFailures.WithLabelValues("_defaults.yaml")
```

**Logger injection** — assert log 內容：

```go
var buf bytes.Buffer
testLogger := log.New(&buf, "", 0)
m := NewConfigManager(dir)
m.SetLogger(testLogger)
m.Load()
if !strings.Contains(buf.String(), "ERROR: skip unparseable") { ... }
```

**FakeClock for WatchLoop** — assert reload 觸發：

```go
fakeClock, stop := startWatchLoopWithFakeClock(t, m, 50*time.Millisecond)
defer stop()
writeTestYAML(t, ...)                  // mutate filesystem
fakeClock.Advance(50 * time.Millisecond) // fire one tick
if !waitFor(t, 2*time.Second, func() bool {
    return m.GetConfig().Defaults["mysql_connections"] == 95
}) { t.Fatal(...) }
```

### t.Parallel 加入決策

新測試**預設加 `t.Parallel()`**。例外（不加）：
1. 用 `time.Sleep` 等真實時間（`config_debounce_test.go` 的 file header 有解釋為何刻意保留 / `config_slow_write_stress_test.go` 是 timing-stress test）
2. 含 `os.Setenv` / `t.Setenv` / `os.Chdir` 等 process-global mutation
3. 含其他全域 swap（`Metrics.` / `slog.SetDefault` / `log.SetOutput` 等）

完整 RISKY tuple 在 `scripts/ops/add_t_parallel.py`。該腳本是大量 `t.Parallel` insertion 的工具，但**手動加 `t.Parallel` 也應該先掃 RISKY tuple**（避免重新引入已知 race）。

### 程式碼指引

| 想找什麼 | 檔案 |
|---|---|
| 三個 setter + lazy getter 定義 | `components/threshold-exporter/app/config.go`（`SetMetrics` / `SetLogger` / `SetClock` + `getMetrics` / `getLogger`） |
| `freshMetrics(t)` helper | `components/threshold-exporter/app/config_metrics_test.go` |
| `startWatchLoopWithFakeClock(t, m, interval)` helper | `components/threshold-exporter/app/watchloop_test.go` |
| `waitFor(t, d, cond)` poll helper | `components/threshold-exporter/app/config_debounce_test.go` |
| RISKY tuple lint | `scripts/ops/add_t_parallel.py` |

## 測試基礎設施

| 檔案 | 職責 |
|------|------|
| `tests/conftest.py` | sys.path 設定 + pytest fixtures（session + function scope）+ Hypothesis profile（見下） |
| `tests/factories.py` | 所有 factory helpers + PipelineBuilder + mock_http_response（含完整 docstring） |
| `pyproject.toml` | pytest markers + coverage config（`testpaths = ["tests"]` 自動遞迴） |

### Hypothesis deadline 慣例（property-based 測試）

**預設不設 deadline。** `tests/conftest.py` 註冊並載入 `vibe` profile（`deadline=None`），所有 `@settings` 未顯式指定 deadline 者一律繼承。

**為什麼**：Hypothesis 預設的 200ms per-example deadline，對**任何碰檔案系統的 example** 量到的是**宿主 I/O 排程**、不是待測性質，而每輪**第一個 example** 還要多付暖機成本；第一次超過門檻、重放時沒有，Hypothesis 就丟 `FlakyFailure: ... Falsified on the first call but did not on a subsequent one`——**時序假象，不是性質被推翻**。`TestFileSha256::test_same_content_same_hash` 斷言「相同內容產生相同 hash」（不可能被證偽）卻會間歇性紅，就是此症狀的證據（實測 290.92ms vs 200ms，重放 2.11ms）。

> 📐 **比例要講清楚，別過度推論**：repo 內共 **116 個 `@given` property**，碰檔案系統或 YAML 的只有 **12 個（約 10%）**。所以「deadline 對本 repo 普遍無意義」是**錯的**——預設不設，是因為出問題的那 10% 沒辦法靠調數字解決，而不是因為多數測試都碰 I/O。
>
> ⚠️ **重現性也要誠實**：此 flake **只在 Windows 開發機、且機器有負載時**重現；同一台機器閒置時曾連 **14 輪全綠**（原廠 deadline）。Linux 容器 5 輪安靜 + 3 輪 `-n 16` 壓力（各 7235 tests）全綠。**不要把它當成該測試的固定失敗率**。

**deadline 仍然有意義的地方要自己寫上去**：顯式 `@settings(deadline=...)` **會覆蓋** profile（已實測）。`shared/test_property_tools.py` 就靠這點讓 82 個 property 中的 79 個保留 `deadline=500`（其中 75 個純函式、4 個是 `monkeypatch` 環境變數測試），deadline 在那裡表達的是「演算法退化」這個真實性質。

判準（**逐測試套用、不是逐檔案**）：

- **碰 I/O** → 不要設 deadline（吃 profile 預設）
- **純函式且想要效能 tripwire** → 顯式 `@settings(deadline=...)` opt-in

⚠️ 注意這是「**預設不設、想要才 opt-in**」，不是「純函式一律要設」。`tests/ops/test_property_based.py` 的 19 個 property（含 15 個純函式）就**全部**吃預設、不設 deadline——那支檔案從來沒有為了效能而設 deadline 的需求。

⚠️ `shared/test_property_tools.py` 的 `PILOT_SETTINGS`（`deadline=500`）原本是**整檔通用**，其中 3 個 property 其實會 `mktemp` + `write_text` + 讀回（`TestLoadYamlFileProperties::test_round_trip`、`TestIterYamlFilesProperties::test_output_sorted_by_filename`、`TestLatestVersionFromChangelogProperties::test_round_trip`），其中一個實測以 `DeadlineExceeded: Test took 916.75ms, which exceeds the deadline of 500.00ms` 紅過——**同一個根因，只是門檻從 200ms 換成 500ms**（916.75ms 是**單一次量測**，歸屬於該次紅的那一個；另兩個是 AST 掃描識別出的同類別）。這 3 個已改用同檔的 `PILOT_SETTINGS_IO`（`deadline=None`）。

**deadline 移除後還剩什麼**：CI 的 `pytest-timeout`（`--timeout=300`，僅 `ci.yml` 跑 pytest 那一個 step）是 **per-test hang backstop**——粒度比 per-example deadline 粗約 1500 倍、抓不到 deadline 想抓的演算法退化，**是不同保證、不是等價替代**。未 suppress 的測試仍受 `HealthCheck.too_slow` 保護。

**臨時要抓時序問題**：`HYPOTHESIS_PROFILE=strict pytest ...` 還原原廠 200ms。

> ⛔ **strict 只對「沒有顯式 deadline」的測試有效。** `test_property_tools.py` 的 82 個 property **全部**都設了 deadline，所以 strict 對該檔是 no-op——**別把該檔在 strict 下全綠讀成「沒有時序問題」**。
>
> 亂填 / 空字串的 `HYPOTHESIS_PROFILE` 會印警告並回退 `vibe`，不會打爛 collection（修正前會讓整棵 `tests/` 中止）。

> ⚠️ conftest 裡的 `from hypothesis import settings` 是**包住的**——有數個 CI job 只裝 `pytest [pyyaml]` 就跑 `pytest tests/...`（`ci.yml` 的 recipe-preview／promtool-goldens／vm-alert-parity step、`vm-anchor-on-pin-change.yml`、`nightly-vm-replay.yaml`、`scripts/ops/federation_e2e_run.sh` 的 venv；**此清單非窮舉，要依賴前請自行複驗**），這支 conftest 對它們一樣會載入，未包住的 import 會直接打爛它們的 collection。
>
> guard 刻意**只吞「hypothesis 本身不存在」**（`ModuleNotFoundError` 且 `name == "hypothesis"`）：若是裝了但相依破損，例外會往上拋，而不是悄悄留在原廠 200ms——後者才是真的 fail-open。

## Factory 清單

| Factory | 用途 | 位置 |
|---------|------|------|
| `write_yaml()` | 寫入 YAML 到暫存目錄 | factories.py |
| `make_receiver()` | 產生 receiver dict（5 types） | factories.py |
| `make_routing_config()` | 產生 routing config | factories.py |
| `make_tenant_yaml()` | 產生 tenant YAML 字串 | factories.py |
| `make_defaults_yaml()` | 產生 _defaults.yaml 字串 | factories.py |
| `make_am_receiver()` | 產生 AM 原生格式 receiver | factories.py |
| `make_am_config()` | 產生完整 AM config dict | factories.py |
| `make_override()` | 產生 per-rule routing override | factories.py |
| `make_enforced_routing()` | 產生 enforced routing config | factories.py |
| `mock_http_response()` | 模擬 HTTP response（urlopen mock） | factories.py |
| `populate_routing_dir()` | 預載多 tenant routing YAML | factories.py |
| `PipelineBuilder` | 鏈式建構 scaffold → routes 管線 | factories.py |

## Test Markers

| Marker | 用途 | 選擇執行 |
|--------|------|---------|
| `slow` | 執行較慢（benchmark, property-based） | `pytest -m "not slow"` 跳過 |
| `integration` | 跨模組整合測試 | `pytest -m integration` |
| `benchmark` | 效能基線測試 | `pytest -m benchmark` |
| `regression` | 已知 bug 回歸 | `pytest -m regression` |
| `snapshot` | 輸出格式穩定性快照 | `pytest -m snapshot` |

## 測試檔案對照

| 測試檔案 | 測試目標 | 測試數 | 備註 |
|---------|---------|--------|------|
| `ops/test_generate_alertmanager_routes.py` | routing / receiver / inhibit / enforced | 142 | 最大功能測試（Wave 13 去重 -13） |
| `ops/test_scaffold_db.py` | RULE_PACKS catalogue / scaffold generation / YAML validation | 129 | parametrize 瘦身後 |
| `ops/test_scaffold_tenant.py` | scaffold_tenant.py 核心功能 | 72 | 覆蓋率 49→62% |
| `shared/test_lib_python.py` | _lib_python 共用函式庫 | 82 | |
| `shared/test_entrypoint.py` | da-tools CLI entrypoint | 24 | monkeypatch 完成 |
| `ops/test_onboard_platform.py` | 完整 onboard 管線 | 71 | parametrize receiver types |
| `ops/test_integration.py` | 跨模組 routing + PipelineBuilder | 17 | integration marker |
| `shared/test_help_contract.py` | 5 個 CLI `--help` 結構契約（flag 存在 / required / choices） | 5 | 取代全文 help 快照（py-version 無關） |
| `ops/test_domain_policy.py` | webhook domain allowlist + fnmatch | 26 | |
| `ops/test_error_consistency.py` | warning format 一致性 | 14 | |
| `shared/test_mutation_guards.py` | 函式行為精確值 | 49 | |
| `ops/test_regression.py` | 已知 bug 回歸 | 9 | regression marker |
| `ops/test_validate_config.py` | validate_config.py 配置驗證 | 25 | Wave 12 unittest→pytest |
| `ops/test_config_diff.py` | config_diff.py 差異偵測 | 40 | Wave 12 unittest→pytest |
| `dx/test_bump_docs.py` | bump_docs.py 六條版號線 + 計數同步 + 七種診斷 × 三個 gating mode | 191 | Wave 12 unittest→pytest；#1407 大幅擴充 |
| `ops/test_init_project.py` | `da-tools init` 產生器：GitLab root 五態分類、子目錄接線、summary 真話 | 238 | #1357 |
| `ops/test_generated_ci_artifacts.py` | 產出的 CI YAML 本身（可解析 / 可達 / image pin / 不含 `git`） | 261 | #1357 / #1358 / #1408 |
| `dx/test_line_ending_policy.py` | 行尾政策：出貨/生產 Python（`scripts`+`components`+`helm`）的寫檔 site 必須明確表態 `newline=` | 292 | ⚠️ 三層：正反例樣本證明偵測器活著／static AST guard 跨平台會紅／行為測試只在 Windows 具鑑別力 |
| `ops/test_maintenance_scheduler.py` | maintenance_scheduler.py 排程 | 55 | Wave 12 mock 統一 |
| `ops/test_performance.py` | 效能曲線（scaling / load） | 7 | slow marker |
| `ops/test_benchmark.py` | 效能基線 | 14 | benchmark + slow markers |
| `shared/test_property.py` | Hypothesis property-based | 15 | slow marker |
| `ops/test_analyze_gaps.py` | analyze_rule_pack_gaps.py gap 分析 | 34 | Wave 15 unittest→pytest + 新增 |
| `ops/test_assemble_config_dir.py` | assemble_config_dir.py 組裝工具 | 34 | Wave 15 unittest→pytest + 新增 |
| `shared/test_validate_all.py` | validate_all.py 驗證入口 | 58 | Wave 16 覆蓋率攻略（14→41%） |
| `ops/test_baseline_discovery.py` | baseline_discovery.py 基線觀測 | 38 | Wave 17 覆蓋率攻略（31→55%） |
| `ops/test_backtest_threshold.py` | backtest_threshold.py 閾值回測 | 39 | Wave 17 覆蓋率攻略（32→70%）+ W18 parametrize |
| `ops/test_batch_diagnose.py` | batch_diagnose.py 批次診斷 | 25 | Wave 17 覆蓋率攻略（49→71%） |
| `ops/test_alert_quality.py` | alert_quality.py 警報品質評估 | 57 | v2.0.0 新功能，89.8% 覆蓋率 |
| `ops/test_policy_engine.py` | policy_engine.py Policy-as-Code 引擎 | 106 | v2.0.0 新功能，94.0% 覆蓋率 |
| `ops/test_cardinality_forecasting.py` | cardinality_forecasting.py 基數預測 | 61 | v2.0.0 新功能，93.5% 覆蓋率 |
| `shared/test_sast.py` | 全倉庫 SAST 合規掃描（6 rules） | 426 | encoding + shell + chmod + yaml.safe_load + credentials + dangerous functions |
| `ops/test_migrate_ast.py` | migrate_rule AST 引擎 | 67 | |
| `ops/test_migrate_v3.py` | migrate_rule v3 引擎 | 38 | |
| `ops/test_blind_spot_discovery.py` | blind_spot_discovery.py 盲區掃描 | 39 | |
| `ops/test_lint_custom_rules.py` | lint_custom_rules.py 規則 lint | 40 | |
| `ops/test_offboard_deprecate.py` | offboard/deprecate 生命週期 | 34 | |
| `ops/test_cutover_tenant.py` | cutover_tenant.py 自動切換 | 26 | |
| `ops/test_patch_config.py` | patch_config.py 局部更新 | 38 | 覆蓋率 54→99% |
| `ops/test_diagnose_inheritance.py` | diagnose 繼承鏈 | 7 | |
| `ops/test_da_assembler.py` | da_assembler 組裝 | 36 | 覆蓋率 48→70% |
| `shared/test_lib_helpers.py` | _lib 輔助函式 | 34 | |
| `ops/test_alert_correlate.py` | alert_correlate.py 警報關聯分析 | 46 | v2.1.0 新功能 |
| `lint/test_check_bilingual_content.py` | check_bilingual_content.py 雙語內容 lint | 24 | v2.1.0 新功能 |
| `lint/test_check_cli_coverage.py` | check_cli_coverage.py CLI 覆蓋率 lint | 29 | v2.1.0 新功能 |
| `lint/test_check_frontmatter_versions.py` | check_frontmatter_versions.py 版號 lint | 29 | v2.1.0 新功能 |
| `dx/test_coverage_gap_analysis.py` | coverage_gap_analysis.py 覆蓋率差距分析 | 22 | v2.1.0 新功能 |
| `ops/test_diagnose.py` | diagnose.py 租戶健康診斷 | 38 | 覆蓋率 40→88% |
| `ops/test_drift_detect.py` | drift_detect.py 配置漂移偵測 | 40 | v2.1.0 新功能 |
| `ops/test_notification_tester.py` | notification_tester.py 通知測試 | 57 | v2.1.0 新功能 |
| `lint/test_snapshot_v2.py` | v2 snapshot 穩定性 | 6 | snapshot marker |
| `ops/test_threshold_recommend.py` | threshold_recommend.py 閾值推薦 | 54 | v2.1.0 新功能 |
| `ops/test_validate_migration.py` | validate_migration.py 遷移驗證 | 49 | 覆蓋率 22→99% |
| `lint/test_check_routing_profiles.py` | check_routing_profiles.py 路由設定檔 lint | 28 | v2.1.0 ADR-007 |
| `ops/test_explain_route.py` | explain_route.py 路由偵錯 | 25 | v2.1.0 ADR-007 |
| `ops/test_generate_tenant_mapping_rules.py` | generate_tenant_mapping_rules.py 租戶映射 | 36 | v2.1.0 ADR-006 |
| `ops/test_scaffold_tenant.py` | scaffold_tenant.py 租戶建立 | 81 | +9 routing profile/topology tests |
| `ops/test_e2e_routing_profile.py` | 路由設定檔 E2E 管線 | 12 | v2.1.0 ADR-007 integration |
| `ops/test_parse_platform_config.py` | _parse_platform_config 解析器單元測試 | 35 | v2.1.0 refactor 驗證 |
| `lint/test_check_doc_freshness.py` | check_doc_freshness.py 文件新鮮度檢查 | 32 | v2.1.0 |
| `lint/test_check_structure.py` | check_structure.py 目錄結構驗證 | 18 | v2.1.0 |
| `lint/test_lint_tool_consistency.py` | lint_tool_consistency.py 工具一致性驗證 | 72 | v2.1.0 |
| `lint/test_check_bilingual_annotations.py` | check_bilingual_annotations.py 雙語標註驗證 | 19 | v2.1.0 |
| `lint/test_check_includes_sync.py` | check_includes_sync.py 中英 include 同步 | 23 | v2.1.0 |
| `lint/test_check_doc_links.py` | check_doc_links.py 文件交叉引用一致性 | 32 | v2.1.0 |
| `ops/test_discover_instance_mappings.py` | discover_instance_mappings.py 1:N 映射自動發現 | 18 | v2.1.0 ADR-006 |
| `ops/test_explain_route_trace.py` | explain_route.py --trace 路由追蹤模擬 | 12 | v2.1.0 ADR-007 |
| `ops/test_byo_check.py` | byo_check.py BYO 整合前檢驗證 | 14 | v2.1.0 |
| `ops/test_federation_check.py` | federation_check.py 聯邦式多叢集驗證 | 18 | v2.1.0 |
| `lint/test_check_repo_name.py` | check_repo_name.py 倉庫名稱一致性 | 14 | v2.1.0 |
| `ops/test_shadow_verify.py` | shadow_verify.py Shadow Monitoring 三階段驗證 | 16 | v2.1.0 |
| `ops/test_offboard_tenant.py` | offboard_tenant.py 安全 Tenant 下架工具 | 22 | v2.1.0 |

## Import 慣例

- Factory helpers：**直接** `from factories import make_receiver, ...`（Wave 13 統一）
- conftest.py 只提供 pytest fixtures（session/function scope），不做 re-export
- 測試檔案不應 `from conftest import` factory 函式

## Snapshot 工作流

JSON 快照位於 `tests/snapshots/*.json`。

- 更新快照：`UPDATE_SNAPSHOTS=1 pytest tests/lint/test_snapshot_v2.py`
- 結構化 diff：整合 deepdiff 顯示差異
- CLI `--help` 已不用全文快照（churn 高、且 py-version 後綴在 CI py3.13 只會 auto-create+skip）；改用 `tests/shared/test_help_contract.py` 結構契約——新增 flag 時同步更新該檔 `HELP_CONTRACTS`

## Benchmark 基線

使用 `pytest -m benchmark` 執行效能基線測試（需 pytest-benchmark）。

| 測試 | v2.0.0-preview.4 基線 | 說明 |
|------|----------------------|------|
| `test_10_tenants` | ~38 µs | 10 tenant routing 產生 |
| `test_50_tenants` | ~197 µs | 50 tenant routing 產生 |
| `test_100_tenants` | ~394 µs | 100 tenant routing 產生 |
| `test_100_tenants` (inhibit) | ~32 µs | 100 tenant inhibit rules |
| `test_10_tenants_from_disk` | ~5.4 ms | 10 tenant 含 YAML I/O |
| `test_parse_integer` | ~102 ns | parse_duration_seconds 微基準 |

基線數據從 Cowork VM 測量（min_rounds=20, warmup=on），用於趨勢偵測而非絕對值。版本升級時更新此表。完整 benchmark 方法論見 [Benchmark Playbook](benchmark-playbook.md)。

## 常用指令

```bash
make test                           # 全量測試（-n auto 平行；CI 同設定；自動遞迴 ops/dx/lint/shared）
make test-serial                    # 循序 + -v（pdb / 確定性順序 debug 用）
make test ARGS="-m 'not slow'"     # 跳過慢速測試
pytest tests/ops/                   # 僅跑 ops 測試（單目錄小子集：serial 就好，見下）
pytest tests/lint/                  # 僅跑 lint 測試
pytest tests/dx/                    # 僅跑 dx 測試
pytest tests/shared/                # 僅跑 shared 測試
make coverage                       # 覆蓋率報告
pytest -m integration              # 僅跑整合測試
pytest -m regression               # 僅跑回歸測試
```

### 平行 vs 循序判斷規則（ROI r6 D 波）

- **全套（或跨多目錄大子集）→ `-n auto`**（`make test` 已是預設）：host 實測全套 serial ~491s vs `-n auto` ~131s（3.8×）。
- **單檔 / 單測試 / 單目錄小子集 → serial**：xdist 啟動開銷 ~2s，小子集平行反而更慢；直接 `pytest tests/ops/test_foo.py` 或 `make test-serial ARGS="-k foo"`。
- **需要 pdb / 確定性測試順序 → `make test-serial`**（xdist 與 pdb 不相容）。
- 依賴：`-n auto` 需 `pytest-xdist`——dev container 由 `postCreateCommand` 預裝；host 端缺它時 `pip install pytest-xdist`（否則 pytest 直接 unrecognized arguments）。
- host 效能門檻類測試（`tests/ops/test_performance.py`）在 Windows host 門檻 ×4（`_SCALE`）——`-n auto` 滿載 CPU 爭用的 wall-clock 噪音不該當假紅；CI ubuntu 門檻不變。

### Go tests（Dev Container；`make dc-go-test`）

Repo root **沒有 go.work**——`go test ./...` 必須逐 module 跑；module 對照表（exporter / tenant-api / am-inhibit，與 ci.yml `go-tests-*` jobs 同步）維護在 `scripts/ops/dc_go_test.sh`。

```bash
make dc-go-test                          # 全部 CI module（exporter + tenant-api + am-inhibit）
make dc-go-test MOD=tenant-api           # 單 module
make dc-go-test PKG=./internal/rbac/...  # 單 package（module 自動推斷；實測 ~7s vs 整 module ~147s）
make dc-go-test MOD=tenant-api ARGS="-run TestX -v"
```

**增量原則**：本機（container）go test 靠 Go build/test cache 天然增量——只重跑受改動影響的 package，**勿加 `-count=1`**。`-count=1` 是 CI-only flag（ci.yml 用它防 cache 遮 flake）；本機加了會放棄增量、每次全量重跑。其餘 CI-only delta：`-race`（本機需要時 `ARGS="-race"`）與 tenant-api 的 `-tags forge_e2e` compile-check（ci.yml 獨立 step）不在 `dc-go-test` 預設內。

⚠️ **Trap #62（worktree 假綠）**：`dc-*` targets 經 dx-run 恆定作用於**主 worktree 掛載**——在 claude worktree 編輯後直接跑會測到主 worktree 的舊 code；單檔同步工作流（cp → 跑 → revert）見 [testing-playbook](testing-playbook.md) §7「Dev Container mount scope」。
