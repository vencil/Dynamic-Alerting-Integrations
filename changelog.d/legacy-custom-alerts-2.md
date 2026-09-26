---
section: Added
topic: custom-alerts
issues: [657]
created: 2026-09-26T17:00:00+00:00
---
- **would-fire 預覽：填個試算值就知道會不會觸發（recipe-preview）**：新增獨立服務 `components/recipe-preview/`，`POST /preview` 以平台同一套 compiler 與 `promtool` 判定一條 recipe 是 firing 還是 inactive；授權一律交給 tenant-api 新增的 `GET /api/v1/tenants/{id}/access` 決定，失敗時 fail-closed 拒絕（[#657](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/657)）。portal recipe-builder 新增試算面板，支援 `threshold` 與 `absence`，其他型別明示尚未支援，並註明預覽只驗閾值邏輯、不代表實際環境會發出通知。新增 `recipe-preview/v*` 發布線（多架構 image、cosign 簽章），helm chart 可實際部署。另修正含單引號 selector 的合法 recipe 被誤報 `error`、授權後端故障原因不可觀測，以及 README 漏列的 timeout 環境變數。
