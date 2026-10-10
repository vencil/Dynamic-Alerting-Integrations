---
section: Added
topic: alertmanager-routing
issues: [2766, 2759, 2700, 2713]
created: 2026-10-10T05:03:45+00:00
---
- **讀取端差異清單擴及路由層與 tenant-api 的 policy 拒收（alertmanager-routing；[#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766)）**：路由對照表的格子在 `python_differs` 記下產生器的值後，與 Go 欄位不同的每個欄位都要在差異清單裡以 `[樹, 租戶, 欄位]` 逐格列出，方向由測試計算；沒列、已消失、方向或格數不符都會紅。[#2700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2700)（租戶檔的 `!!merge` 鍵）與 [#2713](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2713)（被蓋掉的 merge 來源裡有建不出來的值，產生器整棵樹拒收）已加進對照表，列為擋住第二階段的 Go 寬差異。tenant-api 拒收產生器照讀的 policy 檔不再一律放行：快照逐列記錄，方向 `go_refuses`（不算較嚴，因為拒收後會沿用舊 policy，或在 `--policy-unavailable-open` 下放行寫入），依 shape 分組列冊並待 owner 簽核；路由對照表裡 tenant-api 因此對 PUT 回 503、產生器卻接受的格子也列為 `go_refuses`。路由對照表的 `tenant_api` 格只有在量不到時才能是 null，且須在 `tenant_api_unmeasured` 寫明原因。
