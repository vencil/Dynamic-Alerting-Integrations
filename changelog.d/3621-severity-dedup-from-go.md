---
section: Changed
topic: alertmanager-routing
issues: [3621]
created: 2026-11-13T10:30:00+00:00
---
- **⚠️ Breaking：路由產生器的 `_severity_dedup` 只照 exporter 的讀法，新增 `da-guard severity-dedup`（alertmanager-routing；[#3621](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3621)）**：新子命令以 JSON 印出每個租戶的模式（exporter 自己的合併與 `ResolveSeverityDedupLogf`）、exporter 讀不懂的值與寫成排程的值；產生器改讀它決定靜態 inhibit rule，Python 的詞表與合併已刪。讀不懂的值（含平台檔 `tenants:` 條目）與排程寫法仍以 `severity_dedup_invalid` 整棵樹拒收。每次執行比對兩邊的租戶集合，不一致（例如 exporter 整份丟掉一個產生器仍從中載入了租戶的檔；原本回 1）回 2 並指名。沒有時窗的 mapping `{default: disable}` 改照 exporter 的讀法生效（原本拒收；schema 仍拒收）；profile 裡的 `_severity_dedup` exporter 不套用，產生器不判它。da-guard 須與工具同版：沒有 `severity-dedup` 的舊 da-guard 回 2。
