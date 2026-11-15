---
section: Fixed
topic: portal
issues: [3998]
created: 2026-11-15T16:49:37+00:00
---
- **把 conf.d 的租戶檔整份貼進 Config Lint 時，頁面會提示改貼租戶層，不再誤報（portal；[#3998](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3998)）**：Config Lint 把最上層的 key 當成租戶，所以貼上真實的 conf.d 檔（最上層是 `tenants:`）時，它把 `tenants` 當成一個沒有路由的租戶，真正租戶底下的閾值一筆都沒判。現在新規則 `confd-tenants-wrapper` 報一筆警告，請使用者貼 `tenants:` 底下那一層；其他規則對 `tenants` 這個假租戶的 finding 不再顯示（YAML 本身讀不了的檔不辨識，照常報 YAML 錯誤）。另外 Config Lint 的閾值判斷也改為逐列對照 Go 寫出的 `tests/shared/threshold_value_matrix.json` 測試。
