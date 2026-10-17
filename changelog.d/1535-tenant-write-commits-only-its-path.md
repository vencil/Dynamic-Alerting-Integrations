---
section: Fixed
topic: tenant-api
issues: [1535]
created: 2026-10-17T18:30:00+00:00
---
- **租戶寫入只 commit 本次寫的那個檔，validator 改為必填（tenant-api；[#1535](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1535)）**：direct 模式的租戶寫入改從暫時 index 建 commit（HEAD 的樹加上本次路徑，等同 `git commit --only`）。operator 在 conf repo 手動 `git add` 的其他檔不再搭上租戶的 commit，寫入不論成敗都保持 staged；真正的 index 只同步本次路徑，在 HEAD 前進之前更新、失敗時還原，index.lock 被占用時照舊回 503 且不發布。**Breaking（只影響直接使用 `gitops.Writer` 的程式）**：沒有 `SetValidator` 的 Writer 會以 `ErrNoValidator` 拒絕所有寫入，舊的無 validator commit 路徑已刪除；測試改用 `gitopstest.NewWriter`。正式部署本來就接 generator，行為不變。
