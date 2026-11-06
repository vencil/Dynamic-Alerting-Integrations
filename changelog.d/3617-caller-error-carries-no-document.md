---
section: Changed
topic: da-tools
issues: [3617]
created: 2026-11-06T02:00:00+00:00
---
- **⚠️ Breaking：`--json` 在結束碼 2 時，只有「部分答案」才輸出文件（ops 與 lint 工具；[#3617](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3617)）**：早退（參數錯、Prometheus 讀不到、沒有資料、輸入不能用等）的 stdout 改為空，原因只寫 stderr。先前這類情況會輸出一份「沿用該工具 schema、值歸零，加上 `status: caller_error` 與 `reason`」的文件，或 `{"error": …}`；涉及 patch-config、backtest、cardinality-forecast、alert-quality、policy-engine、assemble-config-dir、explain-keys、notification-tester、validate-config、gitops-check、runtime-audit、check-alert 與幾支 lint 工具。只回答了一部分時（例如 operator-check 有一項 kubectl 沒回答），文件照常輸出、結束碼仍為 2。消費端請先看結束碼，再讀 stdout。backtest 的 `-o`／`--markdown-output` 在結束碼 2 時仍寫入未回測報告。
