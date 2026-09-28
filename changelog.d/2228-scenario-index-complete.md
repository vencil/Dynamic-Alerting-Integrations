---
section: Fixed
topic: docs
issues: [2228]
created: 2026-09-28T00:51:10+00:00
---
- **場景索引補齊，README 不再寫場景數（docs；[#2228](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2228)）**：`docs/scenarios/README.md` 補上先前沒列的六個場景（多系統遷移 Playbook、千租戶規模管理、Staged Rule Adoption Lifecycle、版本感知閾值、多域名階層式配置、扁平 `tenants/` → `conf.d/` 遷移決策），各自歸類並附適用角色與摘要；已併入 Shadow Monitoring 的 Shadow Audit 頁改由該列連過去。README 中英兩版原本寫「14 個場景」，與目錄實際頁數對不上，也沒有機制維持，改為不帶數字，並拿掉同一格裡不完整的類別列舉，完整清單以索引頁為準。
