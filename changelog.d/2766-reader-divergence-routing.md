---
section: Added
topic: alertmanager-routing
issues: [2766, 2759, 2700, 2713]
created: 2026-10-10T05:03:45+00:00
---
- **讀取端差異清單擴及路由層（alertmanager-routing；[#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766)）**：路由對照表裡產生器的值（`python_differs`）與 Go 欄位不同的每個欄位，都要在差異清單以 `[樹, 租戶, 欄位]` 逐格列出，方向由測試計算。[#2700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2700)、[#2713](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2713) 已加進對照表，列為擋住第二階段的 Go 寬差異。tenant-api 不在清單裡：它的寫入由產生器判定（[#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)）。
