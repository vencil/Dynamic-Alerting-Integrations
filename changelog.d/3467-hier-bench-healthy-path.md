---
section: Changed
topic: exporter
issues: [3467]
created: 2026-11-22T12:03:02+00:00
---
- **階層 bench 改量健康路徑，被拒值路徑另立一支（bench；[#3467](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3467)）**：hier fixture 的 region 層 `region_alert_schedule` 原本是字串，每個租戶都落入被拒值候選，`FullDirLoad_Hierarchical_1000` 等 bench 量的其實是「整棵樹都有被拒值」的路徑。改成數字後量健康路徑，被拒值路徑由新的 `BenchmarkFullDirLoad_Hierarchical_1000_RejectedValues` 量。⚠️ 這是工作定義的階梯：post-merge 歸因與夜跑的 M/R 比值會跳一次，不是效能改善。
