---
section: Changed
topic: exporter
issues: [2115]
created: 2026-10-06T10:30:00+00:00
---
- **`/effective` 與 `da-guard effective` 的 `effective_config` 改與 `/metrics` 一致：每個閾值一個 key、排程整份取代（tenant-api / da-guard；[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）**：同一個閾值在不同層用新舊兩種拼法寫時（例如根層 `mysql_threads_running`、子目錄或租戶 `mysql_cpu`），過去兩個 key 並列；現在只留 `/metrics` 上勝出那一層的值與拼法，同一層兩種拼法並存時新拼法勝出，`key_sources` 指向那一層。某一層寫的排程（`{default, overrides}`）整份取代下層的值，不再與子目錄排程的時段合併。`merged_hash` 不變。`describe_tenant` 尚未跟進。
