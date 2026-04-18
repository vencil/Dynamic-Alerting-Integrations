---
title: "測試架構導覽 (Test Map)"
tags: [testing, navigation, internal]
audience: [maintainers, ai-agent]
version: v2.7.0
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
└── scenarios/           # Shell 場景腳本
```

v2.7.0 將 98 個 `test_*.py` 從 `tests/` 根目錄搬入 `ops/` / `dx/` / `lint/` / `shared/` 四個子目錄，與 `scripts/tools/` 的分類對齊。`conftest.py` 和 `factories.py` 留在根目錄，pytest 自動遞迴收集子目錄測試。

## 測試基礎設施

| 檔案 | 職責 |
|------|------|
| `tests/conftest.py` | sys.path 設定 + pytest fixtures（session + function scope） |
| `tests/factories.py` | 所有 factory helpers + PipelineBuilder + mock_http_response（含完整 docstring） |
| `pyproject.toml` | pytest markers + coverage config（`testpaths = ["tests"]` 自動遞迴） |

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
| `shared/test_snapshot.py` | 18 個 JSON snapshot | 18 | snapshot marker |
| `ops/test_domain_policy.py` | webhook domain allowlist + fnmatch | 26 | |
| `ops/test_error_consistency.py` | warning format 一致性 | 14 | |
| `shared/test_mutation_guards.py` | 函式行為精確值 | 49 | |
| `ops/test_regression.py` | 已知 bug 回歸 | 9 | regression marker |
| `ops/test_validate_config.py` | validate_config.py 配置驗證 | 25 | Wave 12 unittest→pytest |
| `ops/test_config_diff.py` | config_diff.py 差異偵測 | 40 | Wave 12 unittest→pytest |
| `dx/test_bump_docs.py` | bump_docs.py 版號更新 | 11 | Wave 12 unittest→pytest |
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
| `shared/test_flows_e2e.py` | flows.json E2E 驗證 | 0 | manual-stage marker |
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
| `lint/test_lint_tool_consistency.py` | lint_tool_consistency.py 工具一致性驗證 | 25 | v2.1.0 |
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

快照位於 `tests/snapshots/*.json`，首次執行自動建立。

- 更新快照：`UPDATE_SNAPSHOTS=1 pytest tests/lint/test_snapshot_v2.py tests/shared/test_snapshot.py`
- 結構化 diff：整合 deepdiff 顯示差異

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
make test                           # 全量測試（自動遞迴 ops/dx/lint/shared）
make test ARGS="-m 'not slow'"     # 跳過慢速測試
pytest tests/ops/                   # 僅跑 ops 測試
pytest tests/lint/                  # 僅跑 lint 測試
pytest tests/dx/                    # 僅跑 dx 測試
pytest tests/shared/                # 僅跑 shared 測試
make coverage                       # 覆蓋率報告
pytest -m integration              # 僅跑整合測試
pytest -m regression               # 僅跑回歸測試
```
