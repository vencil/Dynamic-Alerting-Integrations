---
section: Changed
topic: rule-packs
issues: [3778]
created: 2026-11-13T07:03:35+00:00
---
- **⚠️ BREAKING：`mysql_threads_running` 不再有平台預設值（[#3778](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3778)）**：健康的 running threads 數隨主機 CPU 核數而定，平台給不出可攜的值，改為由部署方自己設定：Helm chart 與 dev `_defaults.yaml` 不再帶 30；`da-tools init` 選了 mariadb pack 時仍會在你自己的 `_defaults.yaml` 寫下起始值 30，請依主機調整。沒有設定這個 key 的租戶，`MariaDBHighThreadsRunning`、`MariaDBHighThreadsRunningCritical` 與 `MariaDBSystemBottleneck` 不會觸發；要保留原行為，在 defaults 或租戶設定寫回 `mysql_threads_running: 30`（或依主機核數給值）。`mysql_cpu` 舊拼法的過渡窗不受影響。
