---
section: Fixed
topic: docs
issues: [2482]
created: 2026-09-29T23:41:01+00:00
---
- **Rejection Rate 面板不再把後端 5xx 算成拒絕；安裝文件的 hash 驗證真的會驗到檔案（[#2482](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2482)）**：Federation Audit 與 Tenant Log Query 儀表板的「Rejection Rate (5m)」拿掉 `backend_error`，被拒狀態與對應的 `*RejectionRateAnomaly` 告警相同，並由測試比對。`migration-toolkit-installation` 的下載指令改用原檔名，`sha256sum --check` 才對得上 `SHA256SUMS`；air-gapped 段的版號改由 `TAG` 推出，不再寫死舊版。另更正幾處文件：`==` 對所有非 threshold recipe 都拒絕、`TA_WRITE_MODE=""` 會退回 `direct`、email `to` 可寫成清單。
