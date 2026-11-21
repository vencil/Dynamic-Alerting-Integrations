---
section: Added
topic: tools
issues: [4097]
created: 2026-11-21T02:00:00+00:00
---
- **新的 lint：規則只要保留身分 label，就必須同時保留 `da_cluster`（tools；[#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097)）**：`check_cluster_identity.py` 檢查每條規則的聚合（`by`／`without`）與配對（`on`／`ignoring`）。輸入是 exporter 序列、且寫了 `tenant`、`instance`、`pod`、`namespace`、`node` 任一個卻沒有 `da_cluster` 的，同一個租戶跨兩個叢集時會互相遮蔽或被合併（ADR-006）。運算式由 da-read 的新子命令 `promql-shape` 讀成語法樹，不掃文字。另外檢查「逐叢集的序列用 `on(tenant…)` 配上租戶一份的序列，卻沒有用 `group_left`／`group_right` 把逐叢集那一邊標成多的一側」：同一個租戶有兩個叢集時，Prometheus 對這種算術或比較運算回報 many-to-one 必須明寫、整條規則不產出。現有 rule pack 的 119 處列在 `tests/rulepacks/cluster_identity_baseline.yaml`，清單只能縮小：新增一處會擋下，修掉一處卻沒從清單移除也會擋下。這一版只加檢查，規則本身還沒改。
