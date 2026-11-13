---
section: Fixed
topic: ci
issues: []
created: 2026-11-13T11:09:54+00:00
---
- **每個跑 pytest 的 CI session 都要宣告建不建 Go binary，Playwright 的 `E2E Spec Lint` 不再逾時（ci）**：`tests/conftest.py` 在每個有 `go` 的 pytest session 開頭都先建 da-guard、da-crdecode、da-read。GitHub runner 的映像內建 `go`，冷 cache 下要約 4 分鐘，所以只跑 eslint 與少量 Python 測試的 `E2E Spec Lint (A-13)`（`timeout-minutes: 5`）常被取消。新增 `VIBE_TEST_GO_BINARIES`：`build`（或未設、空值）照舊先建；`skip` 不建，也不設任何 `*_BINARY`；其他值是 usage error。在 GitHub Actions（`GITHUB_ACTIONS=true`）裡，沒有設定這個變數的 pytest session 也是 usage error，連由腳本間接啟動的 pytest（mutation pilot、federation E2E）都會被擋下。另有 lint `tests/lint/test_workflow_pytest_declares_go_binaries.py` 解析 `.github/workflows/` 的 shell 指令，在 CI 執行前就找出沒有宣告的步驟。宣告為 `skip` 的有：`E2E Spec Lint` 與兩支 VictoriaMetrics workflow，這些測試都不讀 Go binary。其餘一律宣告 `build`，行為不變。`skip` 只適合完全不跑需要 binary 之測試的 job：有些測試會自己找 binary（fixture 自建、從 PATH 找、或直接 skip），所以宣告錯了不一定會讓測試失敗。
