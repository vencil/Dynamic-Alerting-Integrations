---
section: Fixed
topic: da-tools
issues: [2724]
created: 2026-10-09T16:30:00+00:00
---
- **`opa-evaluate` 在 OPA 沒有真的評估時改為結束碼 2，不再報「全部通過」（tools；[#2724](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2724)）**：⚠️ 行為變更：連不到 OPA、HTTP 錯誤或轉址、HTTP 回應壞掉、找不到 `opa` 或它失敗／逾時、回應不是 JSON、`<package>.violations` 未定義，以及 `violations` 的項目不是違規物件（字串集合、`severity: null`）時，過去印 `✓ All policies passed.` 以 0 結束（含 `--ci`），現在結束碼 2、stderr 一行 `ERROR:` 指名原因。`--policy-path` 過去每次都評估失敗，現在 input 經 stdin 傳入、查 `data.<package>.violations`；REST 的預設 `--policy-package` 過去打到不存在的路徑，現在 `.` 轉成 `/`。CI 若曾在沒有 OPA 的環境跑 `opa-evaluate --ci`，現在會轉紅。文件補明 `_routing` 不套 `_routing_defaults`、不代換 `{{tenant}}`，以及 `input.defaults` 的實際形狀。
