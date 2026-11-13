---
section: Changed
topic: rule-packs
issues: [3778]
created: 2026-11-13T07:03:35+00:00
---
- **⚠️ BREAKING：`mysql_threads_running` 不再有平台預設值（[#3778](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3778)）**：健康的 running threads 數隨主機 CPU 核數而定，平台不給預設；這個 key 改列在 Helm chart 與 `_defaults.yaml` 的 `optional_overrides:` 宣告清單，租戶檔或部署方的 defaults 皆可設定，租戶單獨寫就會送出。升級影響：原本依賴 chart 預設 30 的部署，升級後這個 key 沒有值，`MariaDBHighThreadsRunning`、`MariaDBHighThreadsRunningCritical` 與 `MariaDBSystemBottleneck` 停響，要自己設（例如 defaults 寫回 `mysql_threads_running: 30`，或依主機核數給值）。`mysql_threads_running_critical` 只在 defaults 層有這個 key 的底值時才送出；只在租戶檔寫 base 時 critical 不生效。`da-tools init` 不再寫入起始值 30，只宣告這個 key。`mysql_cpu` 舊拼法的過渡窗不受影響。
