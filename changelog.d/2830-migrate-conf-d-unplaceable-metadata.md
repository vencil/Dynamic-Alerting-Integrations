---
section: Fixed
topic: dx
issues: [2830]
created: 2026-10-10T14:59:59+00:00
---
- **`migrate_conf_d` 遇到放不了位置的 `_metadata` 改為列出、不再中斷（[#2830](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2830)）**：`_metadata` 寫成字串，或 `domain`／`region`／`environment` 不是字串（例如 `environment: 123`）時，過去整個規劃以 `AttributeError`／`TypeError` 中斷。這兩種寫法 exporter 都讀得到；遷移規劃只讀租戶檔自己的 mapping，所以改為把這種檔標成 `skip_unplaceable_metadata`，在摘要中列出檔名與原因，請手動搬移，其他檔照常規劃。
