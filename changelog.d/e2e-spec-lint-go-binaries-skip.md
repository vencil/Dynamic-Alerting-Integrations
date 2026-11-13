---
section: Fixed
topic: ci
issues: []
created: 2026-11-13T11:09:54+00:00
---
- **每個跑 pytest 的 CI 步驟都要宣告建不建 Go binary，Playwright 的 `E2E Spec Lint` 不再逾時（ci）**：`tests/conftest.py` 在每個有 `go` 的 pytest session 開頭都先建 da-guard、da-crdecode、da-read，runner 冷 cache 下約 4 分鐘，讓只跑 eslint 與少量 Python 測試的 `E2E Spec Lint (A-13)`（`timeout-minutes: 5`）常被取消。新增 `VIBE_TEST_GO_BINARIES`：`build`（或未設、空值）照舊先建，`skip` 不建、也不設任何 `*_BINARY`，需要的測試照樣以 binary 缺失失敗；其他值是 usage error。新 lint `tests/lint/test_workflow_pytest_declares_go_binaries.py` 解析 `.github/workflows/` 的 shell 指令，要求每個執行 pytest 的步驟在 step／job／workflow 的 `env` 寫明 `build` 或 `skip`；`E2E Spec Lint` 設 `skip`，`ci.yml` 的 `lint-rule-packs`、`python-tests-run`、`python-coverage` 設 `build`，兩支沒有 Go 的 VictoriaMetrics workflow 設 `skip`。
