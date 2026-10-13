---
section: Changed
topic: alertmanager-routing
issues: [2904]
created: 2026-10-13T13:04:29+00:00
---
- **只有 da-guard 有的路由檢查搬進 `generate-routes`（[#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904)，ADR-036 §9）**：`routing_in_unread_location`（`_routing*` 寫在 defaults 區塊、沒包 `defaults:` 的 `_defaults.yaml` 頂層或 threshold profile 裡）改由產生器報，`--validate` 擋——⚠️ **變嚴格**：同一棵樹過去 `generate-routes --validate` 回 0，現在回 1，與 da-guard 的 error 一致；只有 `--validate` 擋，單獨 `--strict` 的產出模式印 WARN、照常寫出。`routing_profiles_unusable`、`duplicate_override_matcher`、`redundant_override_receiver` 也由產生器報，維持 da-guard 的 warn：印 WARN、不擋。`conflicting_receiver_field` 的 `field` 改點名多出來的欄位（如 `receiver.routing_key`）。`http_config.proxy_url` 是否為合法 URL 由 `amtool` 判（da-tools 映像有附）；沒有 `amtool` 時 `--validate --strict` 比 da-guard 寬。da-guard 的 finding kind 改為封閉型別（`internal/guard/findingkind`）：kind 只能經由唯一的登記點建立，登記時就標明路由類或閾值類，報告的 JSON 輸出不變；Python 端據此找出還沒有產生器對照的路由檢查，清單不空就紅，作為刪掉 Go 端檢查前的閘門。
