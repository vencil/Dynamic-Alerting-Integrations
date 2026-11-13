---
section: Added
topic: helm
issues: [1269]
created: 2026-11-13T01:10:00+00:00
---
- **只從 git 取用的 chart，打包內容變了就必須升 `version`（pre-commit `chart-version-bump`；[#1269](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1269)）**：兩邊各跑 `helm package`，逐檔比對解開後的內容，所以 Chart.yaml 的註解、`.helmignore` 掉的檔不算，改了預設值、刪了 template 都算。chart 以 `name` 配對，搬目錄不會變成「新 chart」。有發版線 `helm push` 的 chart 只列出、不檢查（它們的版號在發版收尾才調，重打 tag 與 `make chart-push` 仍可讓同一版號對應不同內容，這需要另做發布時的檢查）。版號沒變時，比對的是該版號第一次出現的那個 commit 的打包內容，與 CI 有沒有跑完無關，所以 main 上被後續 push 取代的 run 也不會漏檢；版號變了則必須比 base 高。federation-proxy chart 升到 0.1.4：0.1.3 之後映像升過兩次卻沒升版號，`appVersion` 一併對齊 `v0.16.2`；mariadb-instance 同理升到 0.1.3（Renovate 兩次升映像沒升版號）。Renovate 升 git-only chart 映像的 PR 會因此轉紅，要手動補上版號。
