---
section: Fixed
topic: confd-reader-consistency
issues: [2739]
created: 2026-10-08T20:58:48+00:00
---
- **`describe_tenant.py --what-if` 搭 `--all`／`--diff` 改回 rc 2，副本裡租戶 body 不是 mapping 時具名拒收（tools；[#2739](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2739)）**：⚠️ CLI 行為變更——`--what-if`（不論有沒有帶 `--replaces`）與 `--all` 或 `--diff` 一起用時，以前回 rc 0、輸出跟沒帶 `--what-if` 一樣，看不出模擬根本沒跑；現在回 rc 2，錯誤訊息點名衝突的旗標。`--what-if <副本> --replaces <租戶自己的檔>` 時，副本裡該租戶的 body 若是 list 或純量（`tenants: {tx: [a]}`、`tenants: {tx: a}`），以前 rc 1 並印出 AttributeError traceback；現在與「整份文件不是 mapping」一樣回 rc 2，訊息點名副本檔與租戶。
