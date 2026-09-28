---
section: Fixed
topic: docs
issues: [1666, 1668]
created: 2026-09-28T04:37:29+00:00
---
- **governance-security 的 8080 白名單與基底 image 敘述對齊實作（[#1666](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1666)、[#1668](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1668)）**：tenant-api 8080 的白名單列出全部四組 workload（原文寫兩類，EN 版整段缺漏），並由測試比對 helm values 防再漂移；基底 image 表不再寫死版本、改連到各元件 Dockerfile，修正 nginx 修復敘述叫人 pin 到受影響版本的問題。
