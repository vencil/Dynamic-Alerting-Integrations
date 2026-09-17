---
applyTo: "**/*.go"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-go-code

先讀：
- `docs/internal/test-map.md` §Go tests（Dev Container
- `docs/internal/test-map.md` §測試注入 Seam

約束：Go 測試在 Dev Container（`make dc-go-test MOD=… PKG=…`），且 `dc-*` 恆定作用於主 worktree 掛載——在 worktree 改完直接跑會測到舊 code（Trap #62）；`go vet` 不是 CI 的 linter，推前在容器裡對改到的每個 go module 跑一次 `golangci-lint run ./...`。
