---
section: Changed
topic: threshold-governance
issues: [3017, 3002, 3060, 3088, 3111]
created: 2026-10-16T02:42:15+00:00
---
- **`backtest` 改為回放 alert 實際比較的序列與方向（[#3017](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3017)、[#3002](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3002)）**：每筆變更只查一次 observed-map 記錄的那條序列（`{tenant="…"}`），下界（`<`）key 改數低於門檻的點；取代原本猜的三種查詢名稱與一律 `>` 的計數。⚠️ 判定與結束碼會變：map 沒收錄或無法回放的 key（含 KNOWN_DEFERRED）改標 `not_backtested`、不查 Prometheus，不再可能判 HIGH；critical row 由 `_critical` 的 alert 判讀。每筆結果帶 `query` 欄位（文字與 Markdown 報告也列出）。`N:critical` 照 exporter 的讀法解析，severity 決定由哪支告警比（[#3060](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3060)）：規則不選的 severity（如 `60:high`）讓整個值讀不懂（[#3071](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3071)），`70` → `95:critical` 是 warning 與 critical 兩列（[#3111](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3111)）；與帶維度 row 共用門檻（`max by(tenant)`）的變更不回測。observed-map 缺檔、讀不了、沒有 key 或條目型別不對時結束碼 2。
