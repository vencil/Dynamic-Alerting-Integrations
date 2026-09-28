---
section: Fixed
topic: alertmanager-routing
issues: [2295]
created: 2026-09-28T23:45:00+00:00
---
- **da-guard 與 tenant-api 對重複 key 改照 route generator 整份拒讀（alertmanager-routing；[#2295](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2295)）**：產生器讀 conf.d 時，檔內任一 mapping 出現重複 key 就拒讀整份；yaml.v3 不把 alias key 與其 anchor 並列、同一 mapping 兩個 `<<` 算重複，da-guard 與 tenant-api 以前照 yaml.v3 讀法放行（有的位置甚至讓 domain policy 靜默失效）。現在同一條判定（`pkg/pyyamlcompat.FindDuplicateKey`，以產生器實跑的對照表釘住）在讀檔時先跑：da-guard 對租戶檔、defaults 與根目錄平台檔以 exit 3 點名（stderr 帶出 key 與行號），`_domain_policy`／`_routing_profiles` 檔回 `*_unusable` finding；`PUT /api/v1/tenants/{id}` 與 `POST /{id}/validate` 回與一般重複 key 相同的 400 `invalid YAML`。
