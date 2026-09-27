---
section: Fixed
topic: tenant-api
issues: [1723]
created: 2026-09-27T04:06:15+00:00
---
- **PR 模式寫入沒能切回 base 之後，直接提交的寫入不再落到那條 PR 分支（tenant-api；[#1723](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1723)）**：之前 PR 模式的寫入若兩次都沒能切回 base，工作樹就停在 `tenant-api/…` 分支；之後 groups／views／account registry／federation policy 與 subset，以及 direct 模式的 tenant 寫入，都會 commit 到那條分支並回 200，下一次 PR 寫入切回 base 時這些 commit 就被遺落。現在這類寫入拿到寫入鎖後、讀取任何設定之前會先看目前分支：落在 PR 分支命名空間就把工作樹切回 base，但不論切回成功與否，這一筆都不寫，回 503 並附 `Retry-After`（錯誤碼 `TREE_NOT_ON_BASE`；index.lock 爭用時為 `WRITE_OVERLOADED`）。因此 PR 分支上的內容不會再替 base 回答（例如只在 PR 分支上成立的重複宣告 409、或拿 base 版 hash 比對 PR 分支檔案得到的 409）。切回成功後仍拒絕，是因為這筆請求是對著 PR 分支的樹準備的（client 讀到的內容與 base hash、handler 在寫入前的讀取）；重試會在 base 上重做一遍。為此 direct 模式的 tenant 寫入改在取得寫入名額之後才解析檔案與驗證內容，寫入佇列滿載時，內容有誤的請求會先收到 503 `WRITE_OVERLOADED` 而不是 400。direct 模式停在其他分支或 detached HEAD 時行為不變；⚠️ 但 direct 模式若刻意停在以 `tenant-api/` 開頭的分支，寫入前會被 `reset --hard` 切回 base，工作樹中未 commit 的改動會遺失。
