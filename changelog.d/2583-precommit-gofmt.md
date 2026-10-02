---
section: Added
topic: dx
issues: [2583]
created: 2026-10-02T14:33:44+00:00
---
- **pre-commit 新增 `go-fmt` hook，Go 格式錯誤在本機 commit 就擋下（dx；[#2583](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2583)）**：先前 gofmt 只在 CI 的 Go Lint 跑，沒排版的 `.go` 能通過本機 commit 與 `make pr-preflight-quick`。新 hook 對 staged 的 `.go` 檔（涵蓋所有 Go module）跑 `gofmt -l`，有未排版檔就失敗並列出檔名，不自動改寫；沒有 `.go` 變更時直接 skip。環境沒有 `gofmt`／`go` 時 fail-closed（rc 2，不會顯示成通過）；要跳過請用 `SKIP=go-fmt`，pre-commit 會標成 Skipped，CI 仍會檢查。
