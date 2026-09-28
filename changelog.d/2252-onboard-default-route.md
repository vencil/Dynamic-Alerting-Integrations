---
section: Fixed
topic: alertmanager-routing
issues: [2252]
created: 2026-09-28T04:10:00+00:00
---
- **`onboard_platform.py` 逆向分析時，tenant 的預設路由改取 matcher 最少的那條（[#2252](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2252)）**：先前同一 tenant 有多條路由時後出現者勝；子路由巢狀在 tenant 路由底下時展平後排在後面，會把某條 override 的 receiver 當成 tenant 預設。其餘較窄的路由列入 `SKIP`，原因寫明 per-alert 路由不做逆向。
