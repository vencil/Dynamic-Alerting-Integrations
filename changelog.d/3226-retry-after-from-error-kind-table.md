---
section: Changed
topic: tenant-api
issues: [3226]
created: 2026-10-21T17:04:40+00:00
---
- **tenant-api 三種可重試的錯誤回應補上 Retry-After（tenant-api；[#3226](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3226)）**：錯誤回應改由一張「錯誤種類 → status／code／Retry-After」表產生，`Retry-After` 標頭與 body 的 `retry_after_s` 一律同時出現。因此：forge 熔斷時開 PR/MR 的 503 `FORGE_UNAVAILABLE` 新增 `Retry-After: 5`；`POST /api/v1/federation/tokens` 簽發超過速率上限的 429 `RATE_LIMITED` 新增 `Retry-After`，值為到下一次可簽發的秒數；state store 不可用的 503 body 補上 `retry_after_s`（標頭原本就有）。其餘回應不變。
