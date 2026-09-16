---
name: vibe-paths-go-code
description: "Vibe 路徑觸發指引：動到 components/**/*.go 之前先讀的章節與一句約束。"
paths:
  - "components/**/*.go"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-go-code

先讀：
- `docs/internal/test-map.md` §測試注入 Seam

約束：Go 測試在 Dev Container（`make dc-go-test MOD=… PKG=…`）；`go vet` 不是 CI 的 linter——推前在容器裡跑 `golangci-lint run ./...`（tenant-api 與 threshold-exporter/app 各一次）。
