---
section: Security
topic: tenant-api
issues: [3175]
created: 2026-10-20T17:56:44+00:00
---
- **⚠️ breaking：git 設定指名 filter／diff／merge driver 時 tenant-api 拒絕啟動與寫入，git 指令一律關閉 fsmonitor（tenant-api；[#3175](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3175)）**：這些 driver 由 `.gitattributes` 選檔執行，而 `/diff`、`/validate` 預覽會跑 `git status`，只有讀權限也能觸發。現在 git 讀得到的設定（repo、global、system、include、`GIT_CONFIG_*`；`includeIf` 依情境生效，切換分支或建好批次 worktree 之後、git 碰檔之前，都在 git 接著執行的情境再查一次）有 `filter.*.clean/smudge/process`、`diff.*.textconv/command`、`merge.*.driver` 任一個就拒絕啟動，訊息列 scope 與鍵、不印值；啟動後才出現則寫入與預覽回 500 `INTERNAL_ERROR`。沒有允許清單：用 git-lfs／git-crypt 的 conf repo 不能接 tenant-api，有這類設定的部署升級後會啟動失敗。每個 git 指令帶 `-c core.fsmonitor=false`，`git diff`／`git log -p` 帶 `--no-ext-diff --no-textconv`（diff.external、`GIT_EXTERNAL_DIFF` 不執行）；hooks 照舊。見 [hardening §3.11](docs/api/tenant-api-hardening.md)。
