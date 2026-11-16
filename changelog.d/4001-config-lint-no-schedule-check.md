---
section: Fixed
topic: portal
issues: [4001]
created: 2026-11-15T23:33:08+00:00
---
- **Config Lint 不再把租戶誤當成排程檢查（portal；[#4001](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4001)）**：`parseTenantYaml` 的排程檢查把最上層當成一個租戶的 key，但 Config Lint 的最上層是租戶清單。所以租戶裡有名叫 `default` 的 key、且它連同同層的 `overrides`／`expires`／`reason` 被當成排程解不開時（例如 `default` 是清單，或 `overrides` 不是清單），頁面就報「exporter 拒收整個檔」並略過所有閾值；exporter 實際上照常服務這個檔。閾值那一層真正會被拒收的排程，反而從沒被檢查到。現在 Config Lint 不採用排程檢查（`parseTenantYaml` 新增 `schedules: false` 選項，其他頁面不受影響），回到 [#3974](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3974) 之前的行為：排程寫法在這頁不檢查；語法錯誤與多文件照常回報。
