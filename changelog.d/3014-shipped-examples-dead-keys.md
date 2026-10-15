---
section: Fixed
topic: confd-family
issues: [3014]
created: 2026-10-14T23:53:37+00:00
---
- **出貨範例樹 `conf.d/examples/` 的閾值現在真的上 /metrics（[#3014](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3014)）**：redis／es／mongodb 範例租戶的 base 值原本寫在 `examples/_defaults-multidb.yaml`，這個檔名 exporter 不當 defaults 讀，15 個閾值鍵因此從沒出列。base 改由 threshold registry 生成進根 `_defaults.yaml` 的新區塊 `dev-example-defaults`，該檔刪除；範例裡 5 個沒有任何 rule pack 讀的鍵（`es_cluster_health_status` 等）一併拿掉。新測試要求每棵出貨樹的 `served-values` `unserved` 只能是明列例外（`_routing_profile`、值為 `disable` 的維度鍵）。⚠️ 開發範本的每個租戶都會多出這三個 pack 的 `user_threshold` 列。
