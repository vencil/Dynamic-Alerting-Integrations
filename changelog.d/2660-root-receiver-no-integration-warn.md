---
section: Changed
topic: alertmanager-routing
issues: [2660]
created: 2026-10-03T10:08:59+00:00
---
- **`generate-routes` 在 root receiver 沒有 integration 時印 WARN（[#2660](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2660)）**：`--output-configmap` 與 `--apply` 要寫出／套用的設定，root route 的 receiver 若沒有非空的 `*_configs`，stderr 印一行 WARN 並指向 BYO 整合指南 §11——沒被子 route 接走的告警（含平台自監控告警）會落到那裡、不通知任何人。不是錯誤、`--strict` 不升級、結束碼不變；內建 base 與出貨的 `default` 都是空 receiver，所以預設會印。render 與 `--validate` 不印。
  `explain-route --trace` 的 `receiver_type` 與這道 WARN 共用同一個判定：只有空 list 的 `*_configs`（如 `webhook_configs: []`）現在讀作 `none`，原本會顯示 `webhook`。
