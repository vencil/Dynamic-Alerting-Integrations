---
section: Fixed
topic: tenant-api
issues: [3114, 3113]
created: 2026-10-17T03:34:25+00:00
---
- **tenant-api 交給 git 的路徑一律照字面比對（tenant-api；[#3114](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3114)）**：所有 git 指令都從同一處建立，現在在那裡統一設 `GIT_LITERAL_PATHSPECS=1`。以前 conf.d 路徑或檔名含 `[`、`*`、`?` 時，git 會把它當萬用字元展開：`git add`／`reset`／`checkout` 會連帶動到 conf.d 外名字相近的已追蹤檔，批次 staging worktree 會多 checkout 出那些檔，帳號 id 歷史上限（`git log -p`）會讀到另一個檔的值。觸發條件只有 operator 設定的 conf.d 路徑或檔名本身含這些字元。[#3113](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/3113) 那處的 `:(top,literal)` 前綴在此設定下會被當成檔名的一部分，已改為在 repo 頂層以一般路徑執行。繼承到的 `GIT_GLOB_PATHSPECS`／`GIT_ICASE_PATHSPECS` 會被拿掉（git 拒絕它們與 literal 同時設定）。
