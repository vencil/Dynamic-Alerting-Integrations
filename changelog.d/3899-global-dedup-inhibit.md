---
section: Changed
topic: alertmanager-routing
issues: [3899]
created: 2026-11-14T12:00:00+00:00
---
- **⚠️ Breaking：severity dedup 改為一條全域 inhibit rule，base 不可再放 dedup 規則（alertmanager-routing；[#3899](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3899)）**：產生器不再為每個租戶各產一條，改產一條 `equal: [tenant, metric_group]`、兩側都 gate `tenant` 與 `metric_group` 的規則，規則數不隨租戶數成長；`_severity_dedup: disable`（與沒讀到模式）的租戶列進 target 的 `tenant!~` 排除清單，沒有租戶開著時不產規則，INFO 行改為 `excluded from the severity dedup inhibit rule`（kind `severity_dedup_excluded`）。告警帶 `tenant` 但不在 conf.d 的租戶現在會被 dedup。`--base-config`／`--apply` 的來源若有產生器自己寫出的 dedup 規則（新形狀或 v2.9.0 的 per-tenant 形狀，不看寫法；通常是沒有標記行的舊產物被傳回），結束碼 1、不寫檔；運維自己寫的 inhibit 規則不判、照原樣保留，可能蓋過 `_severity_dedup`：repo 的 base ConfigMap 已刪掉兩條手寫規則（那兩個租戶一旦設成 `disable` 也仍會被 dedup），用舊版副本當來源要先刪掉。Grafana「Dedup Disabled」面板永遠是 0（exporter 對 disable 租戶不送序列），改為「Dedup Enabled」。
