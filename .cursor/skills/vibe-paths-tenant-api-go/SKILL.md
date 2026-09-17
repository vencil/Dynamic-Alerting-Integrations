---
name: vibe-paths-tenant-api-go
description: "Vibe 路徑觸發指引：動到 components/tenant-api/**/*.go 之前先讀的章節與一句約束。"
paths:
  - "components/tenant-api/**/*.go"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-tenant-api-go

先讀：
- `docs/internal/test-map.md` §Go tests（Dev Container

約束：OpenAPI spec 會因為任何 swag 標註可達的 struct 改動而漂移，不只 handler 標註：改完跑 `make api-docs`，再 `git diff --exit-code components/tenant-api/docs/`。CI 上它紅在 `go-tests-tenant-api` job 的「Verify OpenAPI spec is up-to-date」步驟，看起來像 Go 測試失敗。
