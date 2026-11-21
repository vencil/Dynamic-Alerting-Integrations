---
section: Changed
topic: rule-packs
issues: [1320]
created: 2026-11-20T23:30:00+00:00
---
- **⚠️ breaking：DB2 的交易日誌與 tablespace 使用率閾值改為預設啟用（[#1320](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1320)）**：`db2_log_usage_percent`（值 75）與 `db2_tablespace_used_percent`（值 80）原本要租戶自己填才會告警，現在平台直接給值，`DB2HighLogUsage` 與 `DB2TablespaceAlmostFull` 不必設定就會啟用。兩個值取自 IBM 文件記載的 health indicator 預設 warning 門檻（`db.log_util` 75、`ts.ts_util` 80；health monitor 本身已被 IBM 標為 deprecated，這裡只借用數字），公式與 pack 相同。參考起點也跟著變：日誌 70 → 75、tablespace 85 → 80。已經填過值的租戶照自己的值。會新收到告警的主要是刻意配成固定大小、長期高於 80% 的 tablespace：告警取租戶內最滿的那個，無法逐個排除，請調高該 key 或設為 `"disable"`。沒有上限的自動儲存 tablespace 與 infinite logging 仍然沒有序列、不會響。所引的 IBM 頁面只對「不會自動擴充」的 tablespace 寫出預設值，自動擴充且有上限的沿用同一個 80。本版內建表摘要 `791716cf1182`。
