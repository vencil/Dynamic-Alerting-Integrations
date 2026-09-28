---
section: Added
topic: helm
issues: [2153]
created: 2026-09-28T02:24:00+00:00
---
- **threshold-exporter chart 預設帶 startupProbe（helm；[#2153](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2153)）**：exporter 把 conf.d 載入完才啟動 HTTP server，原本只有 liveness probe（延遲 10 秒、每 10 秒一次），冷載入超過約 30–40 秒就會被重啟，重啟後又從頭載入。現在預設的 startupProbe 打 `/health`，`periodSeconds: 10`、`failureThreshold: 60`（最多等 10 分鐘），成功前 liveness 與 readiness 都不執行。載入更久時調高 `startupProbe.failureThreshold`；設 `startupProbe: null` 可拿掉。
