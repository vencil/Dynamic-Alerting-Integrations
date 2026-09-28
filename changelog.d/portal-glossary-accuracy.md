---
section: Fixed
topic: portal
issues: []
created: 2026-09-28T15:55:47+00:00
---
- **互動式術語表對齊實作（portal）**：更正與實作不符的定義——例如 Alert Rule 並非由 tenant YAML 產生（Rule Pack 內建，tenant 只給閾值）、維護模式的單次窗是在 PromQL 過濾而非走 inhibit、500 是 `max_metrics_per_tenant` 的預設值而非固定上限。「Related」按鈕不再指向術語表沒有的條目。新 Vitest 把預設上限、receiver 類型清單與連結目標對唯一來源比對。
