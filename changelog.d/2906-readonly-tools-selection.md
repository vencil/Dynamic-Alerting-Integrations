---
section: Changed
topic: confd-family
issues: [2906]
created: 2026-10-17T14:41:00+00:00
---
- **⚠️ Breaking：唯讀工具改用 parse-tree 的選檔後，各自看得到的範圍變了（tools；[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：`gitops-check local` 算整棵樹的租戶檔，`_` 平台檔讀不了也 fail；`generate_tenant_mapping_rules.py --validate` 的租戶只來自 `tenants:`（平面格式檔名不再當租戶）；`check_threshold_unit_sanity` 以原文讀閾值（`0x1F4` 不是 500），讀不了的檔是 `NOT-READ` 違規；`check_threshold_reachability` 的多文件 defaults 檔是讀不了的檔；`diagnose` 在 parse-tree 拒讀根 defaults 時 `declared` 為空並印 WARN。
