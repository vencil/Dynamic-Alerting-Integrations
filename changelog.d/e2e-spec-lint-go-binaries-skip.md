---
section: Fixed
topic: ci
issues: []
created: 2026-11-13T11:09:54+00:00
---
- **輕量 pytest job 可宣告不建 Go binary，Playwright 的 `E2E Spec Lint` 不再逾時（ci）**：`tests/conftest.py` 在每個有 `go` 的 pytest session 開頭都先建 da-guard、da-crdecode、da-read，runner 冷 cache 下約 4 分鐘，讓只跑 eslint 與少量 Python 測試的 `E2E Spec Lint (A-13)`（`timeout-minutes: 5`）常被取消。新增 `VIBE_TEST_GO_BINARIES=skip`：該 session 不建、也不設任何 `*_BINARY`，需要的測試照樣以 binary 缺失失敗；其他非空值是 usage error。該步驟已設定。
