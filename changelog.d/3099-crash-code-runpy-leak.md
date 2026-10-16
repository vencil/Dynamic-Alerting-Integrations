---
section: Fixed
topic: ci
issues: [3099]
created: 2026-10-16T18:16:56+00:00
---
- **CLI 契約檢查不再把工具的 crash 碼留在行程裡（ci；[#3099](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3099)）**：`check_cli_contract` 與 `check_cli_default_drift` 在同一個行程裡逐支執行工具，`tenant_verify` 會把該行程的 crash 碼設成 1，之後同一個測試 worker 依賴預設碼 2 的測試偶發轉紅。兩支檢查現在每跑完一支工具就還原 crash 碼。
