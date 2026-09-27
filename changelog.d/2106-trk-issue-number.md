---
section: Changed
topic: planning
issues: [2106]
created: 2026-09-27T13:22:16+08:00
---
- **追蹤項目改用 issue 號當編號：`TRK-<issue 號>`（ADR-019；[#2106](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2106)）**：手工看表取號會撞號，而且登記表永遠落後開票。新條目的號碼直接等於追蹤 issue 號，不需要登記，也不會撞號；`planning-id-mapping.md` 的 TRK-300+ 表凍結在 TRK-392，並補上 TRK-392 一列（387／388／389 已由各自的修正 PR 補上）。新 workflow `trk-index-coverage.yaml` 在開票與開／改 PR 標題時，只驗觸發它的那一個標題：表上沒有的三位數新號＝照舊手配，會紅；表上多出 TRK-392 以後的列也會紅（Python Tests）。titles 面改跟 `Link: rel="next"` 翻頁（issues API 對大資料集拒收頁碼式分頁），翻到頁數上限仍有下一頁時回 rc 2。
