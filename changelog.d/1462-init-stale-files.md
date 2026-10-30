---
section: Changed
topic: da-tools
issues: [1462]
created: 2026-10-30T13:25:14+00:00
---
- **`da-tools init --force` 會處理上次產生、這次不再產生的檔（[#1462](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1462)）**：⚠️ 行為變更。以前換設定重跑（拿掉租戶、CI 或部署方式）時舊檔全部留著、rc 0、摘要只列 `(overwritten)`，被拿掉的租戶檔仍會被部署。現在依 `.da-init.yaml` 的檔案清冊找出這些檔：init 上次產生後沒改過的會刪除（conf.d 內經 da-guard write 刪），改過的保留並點名；`--dry-run` 會先列出。舊版 `.da-init.yaml` 沒有清冊時明說量不到、一個都不刪。
