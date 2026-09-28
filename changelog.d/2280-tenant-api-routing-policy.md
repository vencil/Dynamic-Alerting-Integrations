---
section: Fixed
topic: tenant-api
issues: [2280]
created: 2026-09-28T09:16:00+00:00
---
- **tenant-api 的 domain policy 改判解析後的 routing（[#2280](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2280)）**：先前 PUT 只看 body 裡主 receiver 的 type，`_routing_profile` 帶來的 receiver、`overrides` 與 `routes` 都不判。現在 PUT 以根目錄的 `_routing_defaults` 與 routing profile 解析 body 的整份 routing，每個 receiver 都判；batch 的 op 只要 patch 碰到 `_routing_profile` 或 `_routing`，就把 patch 蓋上磁碟上的租戶 block 後判，違反的那筆被排除（PR 模式其餘照常開 PR），只碰其他 key 的 op 不因磁碟上既有的違規被擋。⚠️ 行為改變：`forbidden_receiver_types` 與 `allowed_receiver_types` 分開判，同一個 receiver 可回兩條 violation（先前 forbidden 命中即停）；擋／不擋的判定不變。403 的 `violations[]` 多了 `target` 欄（`receiver` / `overrides[i]` / `routes[i]`）。
