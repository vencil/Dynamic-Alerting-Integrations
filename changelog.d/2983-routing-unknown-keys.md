---
section: Changed
topic: alertmanager-routing
issues: [2983]
created: 2026-10-14T13:40:00+00:00
---
- **產生器不讀的 `_routing*` 鍵改為點名，`--validate` 擋（generate-routes／validate-config／tenant-api；[#2983](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2983)）**：`_routing` 是保留前綴，所以拼錯的 `_routing_profle`、`_routing_defualts` 一律算合法保留鍵，profile 綁定或預設路由就這樣消失，原本 rc 0、沒有任何訊息。現在照確切名稱判定：租戶設定只讀 `_routing`、`_routing_profile`，`_` 檔最上層只讀 `_routing_defaults`、`_routing_enforced`，其他 `_routing*` 鍵逐鍵印 WARN 並附建議，`generate-routes --validate` 與 validate-config 的 `schema` 列回 1（finding kind `routing_key_ignored`），tenant-api 的批次 patch 拒寫；da-guard 不判這一項，樹上有路由內容時列為未判定證據（exit 4）。⚠️ tenant-api 不再接受扁平鍵 `_routing_receiver_type`（產生器從來不讀它），改寫 `_routing.receiver.type`。已寫進租戶檔的這個鍵會讓 CI 的 `generate-routes --validate --strict` 轉紅，tenant-api 對該租戶的其他寫入也會被擋：用批次的 `unset: ["_routing_receiver_type"]` 或不帶它的 PUT 刪掉即可。
