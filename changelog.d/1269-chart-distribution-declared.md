---
section: Changed
topic: release-tooling
issues: [1269]
created: 2026-11-07T16:00:00+00:00
---
- **每個 `helm/*` chart 都必須「有發佈」或「明確宣告只走 git」（lint；[#1269](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1269)）**：`check_chart_pull_surface.py` 多一條規則：沒有 `helm push` 的 chart 必須列在 `GIT_ONLY` 表並寫明理由，否則 pre-commit／CI 擋下；表裡列了實際有推的、或已不存在的 chart 也會擋。目前 7 個 chart 只走 git（federation-*、vector、victorialogs、chargeback-aggregator、mariadb-instance）；da-portal 與 recipe-preview 有 push 步驟但尚未發過版，另列 `UNRELEASED`。文件不再說平台 chart 發佈在不存在的 `ghcr.io/vencil/dynamic-alerting`。
