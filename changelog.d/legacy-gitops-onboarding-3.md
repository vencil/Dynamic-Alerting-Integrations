---
section: Fixed
topic: gitops-onboarding
issues: [1219, 1339, 1351, 1357, 1358, 1408, 1419, 1421, 1444, 1792, 1796]
created: 2026-09-26T17:00:00+00:00
---
- **PR 護欄的報告改為可信（blast-radius、config-diff、da-guard、configmap-assemble）**：blast-radius 過去把閾值變更報成「format-only、無影響」，現在閾值變更歸為 Tier A，無法證明無影響時不再報 Tier C，停掉 pager 的消音一律優先列出（[#1419](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1419)）。init 產出的 GitHub 管線比較基準恆為空的問題已修，`config-diff` crash 改回 exit 2、不再與「有變更」同碼（[#1358](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1358)）；GitLab 腿的 blast-radius job 因映像沒有 `git` 暫時移除。sticky 留言在計算失敗時改顯示 NOT COMPUTED，不再留著舊報告（[#1421](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1421)）。平台自家的 conf.d workflow 與 Dangling Defaults Guard 不再靜默比對空基準或錯的目錄；`configmap-assemble` 改用與 exporter 相同的大小寫不敏感選檔，檔名不再被 shell 二次解析。
