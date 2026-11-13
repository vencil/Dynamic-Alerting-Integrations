---
section: Changed
topic: exporter
issues: [3808]
created: 2026-11-13T00:41:14+00:00
---
- **`da-guard served-values --schedules` 改為逐租戶讀取（da-guard；[#3808](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3808)）**：每個租戶只在自己的排程切點讀、讀完即釋放，不再於整棵樹的每個切點重讀全部租戶並保留每一輪讀數。唯一跨租戶的效應（某時段 /metrics 匯集失敗時所有租戶的每個鍵都帶錯誤段）照舊：先逐租戶找出匯集失敗的時段、取聯集併入每個租戶的切點，錯誤文字仍列出同一時段所有撞 series 的鍵。輸出的 JSON 與先前逐位元組相同，記憶體與時間大幅下降；validate-config 的 `policy_dsl`、config-diff 等讀 `--schedules` 的路徑都受惠。
