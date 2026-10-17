---
section: Fixed
topic: tenant-api
issues: [3114, 3113]
created: 2026-10-17T03:34:25+00:00
---
- **tenant-api 交給 git 的路徑一律照字面比對（tenant-api；[#3114](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3114)）**：以前 conf.d 路徑或檔名含 `[`、`*`、`?` 時，git 會把它當萬用字元展開：`git add`／`reset`／`checkout` 會連帶動到 conf.d 外名字相近的已追蹤檔，批次 staging worktree 會多 checkout 出那些檔，帳號 id 歷史上限（`git log -p`）會讀到另一個檔的值。觸發條件只有 operator 設定的 conf.d 路徑或檔名本身含這些字元。現在帶路徑且不跑 hook 的指令（`add`、`diff`、`log`、`ls-files`、`ls-tree`、只帶路徑的 `reset`）設 `GIT_LITERAL_PATHSPECS=1`；會跑 hook 的檔案 checkout 則在路徑前加 `:(literal)`。**conf repo 裡的 hook 看不到這個設定**：git 會把環境傳給 hook，若對 `commit`／`checkout`／`push`／`fetch`／`update-ref` 等也設，客戶 pre-commit 裡的 `git diff --cached -- '*.yaml'` 會把 `*.yaml` 當字面檔名、比不到任何檔而靜默放行；所以 tenant-api 繼承到的 `GIT_LITERAL_PATHSPECS`／`GIT_GLOB_PATHSPECS`／`GIT_ICASE_PATHSPECS`／`GIT_NOGLOB_PATHSPECS` 一律拿掉，hook 看到的 pathspec 語意與 operator 手動跑 git 時相同。[#3113](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/3113) 那處的 `:(top,literal)` 前綴在 literal 設定下會被當成檔名的一部分，已改為在 repo 頂層以一般路徑執行。
