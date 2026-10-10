---
section: Fixed
topic: dx
issues: [1706]
created: 2026-10-10T20:10:00+08:00
---
- **`validate_all.py --diff-report` 改在拋棄式 worktree 裡跑 fix，不再碰操作者的工作樹（[#1706](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1706)）**：原本每個 fix 跑完都在 repo root 執行 `git checkout .` 還原，會蓋掉 `--assume-unchanged` 的已改檔，以及 fix 執行期間才做的編輯；先前加上的「有未 staged 改動就拒絕」擋不住這兩種。現在以 `git stash create` 取目前工作樹的快照（含未 staged 改動，未追蹤檔另行複製；`--assume-unchanged`／`--skip-worktree` 檔的編輯不在快照內，那些 fix 對 commit 版本跑）建 worktree，fix 與 diff 都在裡面跑，結束後移除；diff 只含 fix 自己的改動，fix 新建的檔也列出。因此不再需要先 `git add -u`；建不出 worktree 時一個 fix 都不跑。
