---
section: Fixed
topic: portal
issues: [2125]
created: 2026-09-27T13:13:17+08:00
---
- **portal 部署明確宣告不提供 simulate，預覽工具不再把錯誤指向 tenant-api（`da-portal` nginx／Helm、simulate-preview；[#2125](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2125)）**：`POST /api/v1/tenants/simulate` 由 threshold-exporter 提供，出貨的 portal 部署沒有代理它——以前這個請求落進 `/api/v1/` 轉給 tenant-api，被當成 tenant id 回 `405`，工具顯示 `HTTP 405 Method Not Allowed` 並叫人去檢查 tenant-api。現在 image 內建的 `nginx.conf` 與 Helm `configmap-nginx.yaml`（Tier-1／Tier-2 都渲染）以 exact-match location 固定回 `501` + `{"code":"SIMULATE_NOT_PROVIDED"}`，安全標頭照常帶上；simulate-preview 只在「501 且 code 相符」時顯示「此部署不提供模擬預覽」，其餘失敗（網路錯、其他狀態碼、非 JSON）顯示通用錯誤。
