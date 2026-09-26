---
section: Added
topic: threshold-governance
issues: [655, 656]
created: 2026-09-26T17:00:00+00:00
---
- **閾值治理主動迴路 `threshold-govern`（Renovate-for-thresholds）**：新增 `da-tools threshold-govern`（預設 dry-run，`--apply` 才寫）與每週 CronJob：篩出腐敗幅度夠大的推薦（`--min-delta-pct` 預設 25），經 tenant-api 為每個租戶開一個可一鍵批准的 proposed-PR；已有 pending PR 即跳過，走獨立的 `X-DA-Write-Source: threshold-governance` 通道，只改被推薦的那一行。配套：跨租戶閾值分布 Grafana dashboard（找出設太嚴或太鬆者）、死人開關告警 `ThresholdGovernanceStale`（治理 Job 連 8 天沒成功即 warning；一輪幾乎全失敗時 Job 以非零結束，告警才會觸發），並列出未治理的 lower-bound `<` 閾值。見 [#656](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/656)、[#655](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/655)。
