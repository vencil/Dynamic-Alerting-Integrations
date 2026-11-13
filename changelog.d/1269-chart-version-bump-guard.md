---
section: Added
topic: helm
issues: [1269]
created: 2026-11-13T01:10:00+00:00
---
- **只從 git 取用的 chart，打包內容變了就必須升 `version`（pre-commit `chart-version-bump`；[#1269](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1269)）**：兩邊各跑 `helm package`，逐檔比對解開後的內容，所以 Chart.yaml 的註解、`.helmignore` 掉的檔不算，改了預設值、刪了 template 都算；版本必須比 base 高。chart 以 `name` 配對，搬目錄不會變成「新 chart」。有發版線 `helm push` 的 chart 只列出、不檢查（它們的版號在發版收尾才調，重打 tag 與 `make chart-push` 仍可讓同一版號對應不同內容，這需要另做發布時的檢查）。PR 上比對與 main 的 merge base；push 到 main 時比對 push 前的 commit，抓兩支 PR 各自升到同一個版號後都合進來的情況。federation-proxy chart 升到 0.1.4：0.1.3 之後映像升過兩次卻沒升版號，`appVersion` 一併對齊 `v0.16.2`。Renovate 升 git-only chart 映像的 PR 會因此轉紅，要手動補上版號。
