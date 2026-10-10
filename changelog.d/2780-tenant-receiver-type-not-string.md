---
section: Fixed
topic: alertmanager-routing
issues: [2780]
created: 2026-10-10T11:23:28+00:00
---
- **租戶的 `receiver.type` 是 mapping 或 list 時不再讓產生器崩潰（[#2780](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2780)）**：主 route、`overrides` 或 `routes` 的 `receiver.type` 寫成 `{a: 1}`、`[a]`，而租戶受 receiver type 約束或 `require_critical_escalation` 管轄時，`generate-routes` 先前直接 traceback，`--findings-json` 只剩 `run_failed`。現在每個檢查都經同一個讀法取 type，非字串（含 `5`、`true`、`~`）一律視為空，與 da-guard 相同。`--validate --strict` 下由 `missing_receiver_field` 擋下並點名該租戶；escalation 照常報 `critical_escalation_missing`。⚠️ 只開 `--strict` 時，非字串 type 不再觸發 `allowed_receiver_types` 違規（`type: 5` 結束碼由 1 變 0）；集合 type 遇 forbidden／allowed 由 traceback 變 0。
