---
section: Changed
topic: tenant-api
issues: [2900, 2858, 2486]
created: 2026-10-11T20:28:00+00:00
---
- **⚠️ breaking：tenant-api 批次改為全有全無、一次 commit（tenant-api；[#2900](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2900)、ADR-036 §4）**：`POST /tenants/batch` 與 `POST /groups/{id}/batch`（直寫與 PR 模式、同步與非同步）在另開的暫存 git worktree 逐筆套用、仍過今天的檢查，全部通過才寫成**一個** commit（PR 模式是只有一個 commit 的 PR/MR）；讀者不會看到從未 commit 的內容（先推進分支、再更新檔案；中途崩潰在下次寫入或啟動時補完）。任一筆被拒（含 PR 模式在最新 base 上判出的）就整批不寫：回 **422**、`status: rejected`，被拒的是 `status: error`，其餘是新狀態 `not_applied`；非同步 task 改為 `failed` 並保留逐筆結果。過去會部分成功，依賴它的呼叫端要改看 422，修正後重送整批。直寫模式的 409 `CONFLICT` 以 `committed` 區分「什麼都沒寫」與「已 commit」。
