---
section: Changed
topic: exporter
issues: [1425]
created: 2026-10-31T02:45:39+00:00
---
- **threshold-exporter chart：新增 `values.schema.json`，chart 沒有的 key 直接讓 helm 失敗（breaking，[#1425](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1425)）**：以前拼錯的 key、已移除的舊 key（例如 [#2073](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2073) 拿掉的 `rules.operator.ruleLabels`／`receiverTemplate`／`secretRef`，直接沿用 2.9.0 整份 values.yaml 當覆寫檔就會帶到），或對舊版 chart 設了新版才有的 key，都被靜默忽略、rc 0。現在 `helm install`／`upgrade`／`template` 會以 `additional properties` 失敗並指出路徑。只約束 key 集合；root 放行 helm 自己的 `global` 與 umbrella chart 以 `condition: threshold-exporter.enabled` 開關時帶進來的 `enabled`；`thresholdConfig.tenants`／`defaults`／`state_filters`、`service.annotations`、`resources`、各 probe、`rules.operator.serviceMonitor.labels` 保持開放，文件允許的 `null` 照舊可用。⚠️ 遷移：升級前從 values 檔與 `--reuse-values` 帶入的設定中移除 chart 已不認得的 key。
