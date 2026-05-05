---
title: "Tests — 起手式速查"
purpose: |
  這份是 contributor 第一次進 tests/ 看的 README——目的是 30 秒內回答：
  「我要寫的測試該擺哪、怎麼跑、會不會被 CI 跑到」。

  深入內容（factory 清單、E2E 矩陣、CI job 細節、debug 流程）請看 doc-map：
    - docs/internal/test-map.md       — 目錄結構 + factory inventory + marker 表
    - docs/internal/testing-playbook.md — 排錯手冊（CI flake / Go race / Playwright timeout）
    - docs/internal/test-coverage-matrix.md — E2E 場景 × 功能域覆蓋矩陣
audience: [contributors, ai-agent]
lang: zh
---

# Tests — 起手式速查

## 目錄結構（一張圖）

```
tests/
├── conftest.py / factories.py    # 全域 sys.path + factory helpers
├── ops/        (55 檔)            # scripts/tools/ops 對應 unit tests
├── dx/         (8 檔)             # scripts/tools/dx 對應 unit tests
├── lint/       (20 檔)            # scripts/tools/lint 對應 unit tests
├── shared/     (15 檔)            # 跨類別 / 基礎設施 / property-based
├── e2e/        (23 specs)         # Playwright E2E (TypeScript)
│   └── fixtures/                 # diagnostic-matchers / axe-helper / mocks
├── e2e-bench/                    # Playwright E2E benchmark (有自己的 README)
├── fixtures/                     # Python 共用測試資料
├── golden/                       # Golden file 比對基準
├── snapshots/                    # JSON / snap 快照
└── scenarios/                    # Shell 場景腳本（make test-scenario-* 入口）
```

Go 測試**不在** `tests/`，而是與被測程式碼同目錄：
- `components/tenant-api/internal/*/[*]_test.go`
- `components/threshold-exporter/app/*_test.go`

## 怎麼跑（local cheat sheet）

| 想做什麼 | 指令 |
|---------|------|
| 跑全部 Python 測試 | `make test` |
| 跑特定測試 | `make test ARGS="-k <pattern>"` |
| 看覆蓋率 | `make coverage`（HTML：`ARGS="--html"`） |
| 跑 Playwright E2E | `make test-e2e` |
| 跑 Playwright 單一 spec | `make test-e2e ARGS="saved-views.spec.ts"` |
| 跑 Go 測試 | `make dc-go-test`（必須 dev container；host 端 Go 不可用） |
| Skip 配額審計 | `make test-skip-audit`（budget=5） |

**Cowork VM 限制**：Python tests 可直接跑；Go tests **只能在 dev container 內**（`make dc-go-test` 或 `docker exec vibe-dev-container go test ...`）。

## CI 對應（哪個 job 跑哪個目錄）

| Job (workflow) | 跑什麼 |
|---------------|--------|
| `Python Tests (3.13)` (ci.yml) | `pytest tests/ scripts/tools/`，跳過 3 個 `@slow` 測試（見下） |
| `Go Tests (1.26)` (ci.yml) | `go test ./cmd/... ./internal/...`（tenant-api）+ `./...`（threshold-exporter） |
| `Smoke Tests (Chromium)` (Playwright E2E workflow) | `tests/e2e/*.spec.ts` |
| `Nightly Race Detector` (nightly-race.yaml) | Go `-race -count=10`，advisory only（TD-026） |
| `Lint Documentation` (ci.yml) | `make lint-docs`（含 changelog / link / structure check） |

**CI 跳過的慢測試**（local 用 `make test` 會跑）：
- `tests/shared/test_property.py` — Hypothesis property-based（`@slow`）
- `tests/ops/test_benchmark.py` — 效能基準線（`@benchmark @slow`）
- `tests/shared/test_pipeline_integration.py` — End-to-end pipeline

## 我要寫新測試該擺哪？（決策樹）

```
測試 scripts/tools/ops/foo.py?       → tests/ops/test_foo.py
測試 scripts/tools/dx/foo.py?        → tests/dx/test_foo.py
測試 scripts/tools/lint/foo.py?      → tests/lint/test_foo.py
測試跨檔案 / 基礎設施?                → tests/shared/test_<topic>.py
測試 Go 程式碼?                       → 同檔案旁 *_test.go（不在 tests/）
測試使用者操作 portal UI?              → tests/e2e/*.spec.ts
測試前端跨工具效能 / 頁面載入?         → tests/e2e-bench/
測試需要真實 DB / Prometheus / docker? → tests/scenarios/ + Makefile target
```

如果不確定就放 `tests/shared/`，PR review 時再 relocate。

## 共用 fixture / helper

- **Python**：所有 factory 在 `tests/factories.py`（`write_yaml`、`make_tenant_yaml`、`PipelineBuilder` 等，全部有 docstring）。新測試**不要**自己寫 helper，先看 factories.py。
- **E2E**：`tests/e2e/fixtures/` 含：
  - `diagnostic-matchers.ts` — `toBeVisibleWithDiagnostics()`，失敗時 dump 所有可見 testid
  - `axe-helper.ts` — WCAG 2.1 AA 檢查（`checkA11y` / `formatA11yViolations` / `waitForPageReady`）
  - `portal-tool-smoke.ts` — `runToolSmokeChecks()`，含 axe 內建
  - mock 慣例：`page.route('**/api/v1/...')`，每 test 在 `loadXxx` 之前 mock

## Test marker 速查

```python
@pytest.mark.slow         # local 跑，CI 跳過
@pytest.mark.benchmark    # 效能測試，需要乾淨環境
@pytest.mark.golden       # golden file 比對
```

完整 marker 清單與用法 → [test-map.md §Test Markers](../docs/internal/test-map.md#test-markers)。

## 排錯起手式

CI 失敗或 local flake → 查 [testing-playbook.md](../docs/internal/testing-playbook.md)，常見問題：

- Go race detector flake → §LL §3
- Playwright timeout → §LL §4 / §10
- pytest fixture order issue → §LL §5
- E2E selector regression → §LL §11（cold-start contract）

## 不要做的事

- ⛔ **不要** hardcode tenant id（`db-a` / `db-b`）— 用 factory 產生 random id（dev-rule #2）
- ⛔ **不要**自己寫 helper，先看 `factories.py` / `tests/e2e/fixtures/`
- ⛔ **不要**對沒理由的 `time.Sleep`，用 ticker poll until condition（TD-019 / TD-024 模式）
- ⛔ **不要**在 Go test 用 `assert.NoError` 在 setup 階段（fail-fast 用 `require.NoError`，TD-023）
