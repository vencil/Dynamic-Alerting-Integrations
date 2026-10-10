---
section: Added
topic: alertmanager-routing
issues: [2766, 2759, 2700, 2713]
created: 2026-10-10T05:03:45+00:00
---
- **讀取端差異清單擴及路由層與 tenant-api 的 policy 拒收（alertmanager-routing；[#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766)）**：路由對照表裡產生器的值（`python_differs`）與 Go 欄位不同的每個欄位，都要在差異清單以 `[樹, 租戶, 欄位]` 逐格列出，方向由測試計算。[#2700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2700)、[#2713](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2713) 已加進對照表，列為擋住第二階段的 Go 寬差異。tenant-api 的 PUT 判定逐格比對產生器以只看 conf.d 的 `--validate --strict` 執行的 findings 文件（同 `make validate-routes`；`--policy` 不在範圍，見 #2826）：產生器擋下而 tenant-api 放行的格列為 Go 寬；`tenant_api` 格只有量不到時才能是 null，原因寫在 `tenant_api_unmeasured`。tenant-api 拒收產生器照讀的 policy 檔另列方向 `go_refuses`，待 owner 簽核。[未驗] batch：12 個 `tenant_api.batch` 子格沒有比對產生器，數目由測試釘住。
