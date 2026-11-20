---
section: Changed
topic: tools
issues: [4064]
created: 2026-11-20T16:56:13+00:00
---
- **宣告的 YAML／JSON 輸入，同一個鍵寫兩次即結束碼 `2`（tools；[#4064](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4064)）**：⚠️ breaking。先前 PyYAML 與 `json` 都靜默取後寫的值：lint 政策 `denied_functions: [x]` 後接 `denied_functions: []` 就不再禁止 x，部署端 policy 後接 `allowed_domains: ['*']` 就放行任何網域。現在訊息點名旗標、路徑與鍵，YAML 另附兩個行號。鍵的比對沿用 conf.d 的 strict 讀法（yaml.v3 的節點種類＋原文，`<<` merge 帶進來的鍵不算，同一個 mapping 只能有一個 `<<`）。metric dictionary（`migrate`、`onboard`、metric dictionary lint）與 dashboard（`grafana-import --dashboard-dir` 及其 ConfigMap 檢查）的其他讀法一併改成相同判法。
