---
section: Added
topic: helm
issues: [1269]
created: 2026-11-13T01:10:00+00:00
---
- **只從 git 取用的 chart，打包內容變了就必須升 `version`（pre-commit `chart-version-bump`；[#1269](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1269)）**：兩邊各跑 `helm package`，逐檔比對解開後的內容，所以 Chart.yaml 的註解、`.helmignore` 掉的檔不算，改了預設值、刪了 template 都算；版本必須比 base 高。有發版線 `helm push` 的 chart 會列出並略過，因為它們的版號由 tag 保證唯一。PR 上比對與 main 的 merge base，main 上則比對前一個 commit，抓兩支 PR 各自升到同一個版號後都合進來的情況。federation-proxy chart 升到 0.1.4：0.1.3 之後映像升過兩次卻沒升版號，`appVersion` 一併對齊 `v0.16.2`。Renovate 升 git-only chart 映像的 PR 會因此轉紅，要手動補上版號。
