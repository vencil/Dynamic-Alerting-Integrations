---
section: Added
topic: platform-self-monitoring
issues: [962, 1242]
created: 2026-09-26T17:00:00+00:00
---
- **告警平面自我存活性：Watchdog 心跳與 CronJob、config reload 盲區補齊（platform、tenant-api；[#1242](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1242)）**：依 ADR-025 新增永遠 firing 的 `Watchdog` 與置頂專線路由，把心跳送到 operator 自備的外部 dead-man's-switch（URL 走 Secret `url_file`），並以 `AlertmanagerWebhookNotificationsFailing` 區分「平台死了」與「心跳管路壞了」；Watchdog 不可被 inhibit 由產生器 fail-closed 驗證。新增 `CronJobLastRunFailed`，補上 CronJob 從未成功過就零偵測的盲區，最近一次成功即自動 resolve。tenant-api 新增 `tenant_api_config_reload_failures_total` 與 `tenant_api_config_last_reload_successful`，以及 `TenantApiConfigReloadFailing`／`TenantApiConfigReloadStale` 告警，讓壞 config 被靜默保留 last-good 的情況可見。另提出 Runtime Canary 設計與 CI demo，常駐部署仍 defer。
