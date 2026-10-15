---
section: Changed
topic: alertmanager-routing
issues: [2960]
created: 2026-10-14T00:43:25+00:00
---
- **路由區塊裡產生器不讀的鍵會被點名，`--validate` 擋（generate-routes／validate-config／da-guard；[#2960](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2960)）**：租戶 `_routing`、routing profile、`_routing_defaults`、`_routing_enforced`（含其下 receiver、`overrides` 條目、`routes` 條目的 receiver）寫了 `continue`、`mute_time_intervals`、`matchers` 這類產生器不渲染的 Alertmanager 欄位或拼錯的鍵時，產出與沒寫時相同，原本 rc 0、沒有任何訊息。現在逐鍵印 WARN 並附建議，`generate-routes --validate` 與 validate-config 的 `schema` 列回 1（在 da-tools 映像裡也生效，不靠 jsonschema）；render、`--output-configmap`、`--apply` 照常產出。da-guard 報 error `routing_key_ignored`。`http_config` 底下不看。⚠️ 已有這類鍵的設定，升級 da-tools 映像後 CI 的 `generate-routes --validate --strict` 會轉紅：照訊息刪掉或改正那個鍵。
