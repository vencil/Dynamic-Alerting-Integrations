---
section: Fixed
topic: tenant-api
issues: [1723]
created: 2026-09-27T04:06:15+00:00
---
- **PR 模式寫入沒能切回 base 之後，直接提交的寫入不再落到那條 PR 分支（tenant-api；[#1723](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1723)）**：之前 PR 模式的寫入若兩次都沒能切回 base，工作樹就停在 `tenant-api/…` 分支；之後 groups／views／account registry／federation policy 與 subset，以及 direct 模式的 tenant 寫入，都會 commit 到那條分支並回 200，下一次 PR 寫入切回 base 時這些 commit 就被遺落。現在這類寫入提交前會先看目前分支：落在 PR 分支命名空間就先切回 base 再寫；切不回去，或目標檔在 base 上的內容與分支上不同（寫入內容是依分支上的版本算出來的），就什麼都不寫，回 503 並附 `Retry-After`（錯誤碼 `TREE_NOT_ON_BASE`；index.lock 爭用時為 `WRITE_OVERLOADED`），重試即可。direct 模式停在其他分支或 detached HEAD 時行為不變。
