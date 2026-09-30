---
section: Fixed
topic: helm
issues: [2532]
created: 2026-09-30T14:28:00+00:00
---
- **chart 宣告了卻沒有 template 讀的 values key，改為真的生效或移除（helm；[#2532](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2532)）**：federation-reconciler 的 `nodeSelector`／`tolerations`／`affinity` 先前設了不會進 Deployment，現在會套到 pod spec；預設值（空）渲染結果不變。da-portal 的 `serviceAccount.create`／`serviceAccount.name`（ServiceAccount 一律以固定名稱建立）與 victorialogs 的 `serviceMonitor.enabled`（chart 沒有 ServiceMonitor；抓取走 `prometheusScrapeAnnotations`）從 values.yaml 移除——這三個 key 從來沒有作用，移除後渲染結果不變，values 檔裡留著也不會報錯，但不會生效。
