---
section: Changed
topic: tenant-api
issues: [2903, 2486, 2280]
created: 2026-10-15T13:57:48+00:00
---
- **⚠️ breaking：tenant-api 不再自己判 routing 與 domain policy，只由產生器判（ADR-036 §4；[#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)）**：policy 違規、receiver 形狀、不是 mapping 的 `_routing`、matcher／`group_by` 的值、產生器不讀的鍵、`_domain_policy.yaml` 讀不了，一律是 422 `GENERATOR_FINDINGS`。移除 403 `POLICY_VIOLATION` 與 tenant-api 自己的 503 `POLICY_UNAVAILABLE`（產生器回報部署端 policy 檔不可用的 503 照舊）。PUT 這幾類不再回 400，batch 寫非停用的 `_routing` 不再回 400，escalation 洩漏不再附在 `warnings`，`tenant_api_config_*{component="policy"}` 移除。Go 端只留扁平鍵 `_routing_receiver_type` 與遮罩佔位字串兩項 400。
