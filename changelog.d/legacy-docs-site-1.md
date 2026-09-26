---
section: Fixed
topic: docs-site
issues: [1185, 1267, 1268, 1277, 1540, 1665, 1884]
created: 2026-09-26T17:00:00+00:00
---
- **文件站可達性：首頁 404、nav 漏收與錨點連結（docs、ci）**：GitHub Pages 服務來源與 CI 部署的產物脫節，整站根路徑與 `/en/` 長期 404；部署改走 GitHub Actions 原生 Pages 管線，讓上線的就是 lint 驗過的同一份 `mkdocs build`，並新增 `pages-health` 每日檢查服務來源與首頁 HTTP 200（需一次性把 Pages 來源切到「GitHub Actions」）。nav 補上 58 份已發布卻沒有導覽入口的文件（其中 3 份原本完全走不到），新增「設計深潛」子節；之後漏收文件會讓 mkdocs strict 檢查失敗。連結檢查器的標題 slug 改為與 GitHub 一致，修好 45 條先前靜默點不到的錨點連結。README、BYO Prometheus 整合指南、架構文件與 rule pack 設計文件裡手抄的計數改為生成或移除，雙語 tool-map／doc-map 兩種語言都納入 drift 檢查。詳 [#1884](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1884)、[#1267](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1267)、[#1277](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1277)。
