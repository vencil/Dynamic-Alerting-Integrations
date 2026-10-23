---
section: Changed
topic: da-tools
issues: [3133]
created: 2026-10-23T05:00:00+00:00
---
- **⚠️ 結束碼變更：check-alert、blind-spot、alert-correlate、operator-check、shadow-verify 讀 alerts／targets／Alertmanager 狀態沒得到回答時結束碼 2（[#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133) 第三批）**：lib 新增 `prometheus_alerts`、`prometheus_targets`、`alertmanager_status`，回應格式由它們檢查。check-alert 遇到缺 `data` 先前報 `inactive`（0 → 2）；blind-spot 讀不到 targets 先前把每個 DB 報成盲區（0 → 2）；alert-correlate 讀不到 Prometheus 的 alerts 先前以零告警繼續（0 → 2），推出來的 Alertmanager URL 讀不到時印 `NOTICE` 改讀 Prometheus。operator-check 與 shadow-verify 只在明確給了 `--prometheus`／`--alertmanager` 時才算：給了卻讀不到，該項 FAIL、結束碼 2（先前是 warn／skip）；沒給則維持原狀。
