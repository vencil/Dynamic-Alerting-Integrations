---
section: Fixed
topic: alertmanager-routing
issues: [1095]
created: 2026-09-26T17:00:00+00:00
---
- **sentinel 不再送進通知通道、email receiver 產出合法組態（alertmanager）**：四條 severity=none 的 sentinel（如 `TenantSeverityDedupEnabled`）先前會被租戶與 NOC route 接住，dedup 預設啟用的租戶每個 repeat_interval 都收一封通知；現在所有 sentinel 帶 `component="sentinel"`，由新增的平台 sinkhole route 吸收，inhibit 與 silent 行為不變（[#1095](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1095)）。GitOps 渲染對 email receiver 產出的 `to` 列表與缺少的 `from` 會被 Alertmanager 拒收；現在 `to` 轉成字串、`from` 成為必填欄位，CI 另以 `amtool check-config` 驗證渲染結果。try-local 的 severity dedup inhibit 規則改為平台實際部署的直接 critical→warning 規則。
