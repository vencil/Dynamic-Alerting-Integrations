---
section: Added
topic: rule-packs
issues: [2789]
created: 2026-11-22T04:20:38+00:00
---
- **新告警 `TenantProjectionStampMissing`：租戶 audit 列「該蓋章卻落進 `0:0`」（rule-packs、helm/vector 0.14.1；[#2789](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2789)）**：ADR-021 決定、先前沒做的結構性檢查。Vector 新增唯讀旁支 `tenant_stamp_check` → `tenant_stamp_missed`，在 9598 輸出 `vector_tenant_projection_stamp_missed_events_total{reason}`：帶合法 `account_id`（logs token 已驗章的 claim）、請求確實轉給了日誌庫，卻沒進任何租戶分區的列，`not_projected`（`tenant_id` 空白或不在 `tenantProjections`）或 `origin_rejected`（被改判 `suspicious_audit`）。兩個 reason 開機即以 0 初始化並常駐，告警以 `increase(...[1h]) > 0` 判定，抓取間隔沒有額外上限。不比 baseline，1 小時內有一列就發 warning，不自動切斷查詢。⚠️ 已有租戶持 logs token 卻沒列進 `tenantProjections` 的部署，升級後會立刻收到這條告警；`helm/vector` 未到 0.14.1 時第一筆看不到（0.13.1 以前 counter 不存在）。`TenantProjectionFanoutDiscardSpike` 保留。
