---
section: Fixed
topic: exporter
issues: [2414]
created: 2026-09-30T12:14:00+00:00
---
- **子目錄 `_defaults.yaml` 不再以另一種拼法蓋掉租戶自己的閾值（exporter / da-guard；[#2414](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2414)）**：租戶檔寫 `mysql_cpu`、上層子目錄 `_defaults.yaml` 寫 `mysql_threads_running` 時，過去 `/metrics` 送的是子目錄的值而不是租戶的值。現在「租戶是否已寫這個閾值」與子目錄各層之間的覆蓋都以閾值（不分拼法）判斷：租戶寫任一拼法都勝出；沒寫時由最深一層勝出，同一層兩種拼法都寫則新拼法勝出。`da-guard` 的「覆寫多餘」建構 defaults chain 時套用同一規則，不再把「刪掉會讓 `/metrics` 改值」的覆寫判為多餘。`/effective` 與 `describe_tenant` 的 effective config（及 `merged_hash`）不變，仍保留各層原本的拼法。
