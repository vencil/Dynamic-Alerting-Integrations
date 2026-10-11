---
section: Changed
topic: tenant-api
issues: [2900, 2858, 2486]
created: 2026-10-11T20:28:00+00:00
---
- **⚠️ breaking：tenant-api 批次改為全有全無、一次 commit（tenant-api；[#2900](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2900)、ADR-036 §4）**：`POST /tenants/batch` 與 `POST /groups/{id}/batch`（直寫與 PR 模式、同步與非同步）每筆仍過今天的檢查，全部通過才把改到的檔寫進**一個** commit（PR 模式是一支只有一個 commit 的 PR/MR）。任一筆被拒就整批不寫：回 **422**、`status: rejected`，被拒的是 `status: error`，其餘是新狀態 `not_applied`；非同步批次的 task 改為 `failed` 並保留逐筆結果。過去會部分成功（直寫回 200 且已寫入通過的那幾筆、PR 模式略過被拒的那筆照開 PR），依賴這行為的呼叫端要改成看 422，修正被拒的那筆後重送整批。整批層級的失敗（佇列滿、commit 失敗等）也不留下任何改動。不存在的群組成員仍是 `skipped`，不使整批失敗。
