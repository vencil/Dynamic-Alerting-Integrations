---
section: Changed
topic: ci
issues: [1443]
created: 2026-11-06T17:07:07+00:00
---
- **移除 e2e-bench 兩棵合成 fixture 樹裡被追蹤的佔位 `_defaults.yaml`（ci；[#1443](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1443)）**：`scripts/ops/bench_e2e_run.sh` 每次都刪掉並重新生成這兩個檔，所以裡面的值從來沒有被讀過；而且在租戶檔已生成的情況下 `git restore` 它，會造成佔位 defaults 和舊租戶混在一起的樹。整棵 `fixture/synthetic-v1/`、`synthetic-v2/` 改為完全 gitignore。`check_threshold_reachability` 不再登記這兩個 root，也退役了只為它們存在的排除機制（`_ARTIFACT_KEYS_FLOOR_EXCLUDED_*`）；artifact key floor 的分母從此就是整個掃描範圍，floor 數值不變。#1443 指出的「加一行豁免、就沒有任何其他檢查看著」的 root 只剩這一棵，因此不再為豁免清單另加守衛。
