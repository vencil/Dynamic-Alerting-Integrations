---
section: Removed
topic: da-tools
issues: [3376, 3348]
created: 2026-10-28T15:00:00+00:00
---
- **⚠️ BREAKING：`gitops-check` 只留 `local`，移除 `repo`、`sidecar` 子命令與 `--ci`（[#3376](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3376)、[#3348](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3348)）**：兩個子命令檢查的是 GitOps Native Mode，自 [#1349](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1349) 起不支援——chart 不部署 git-sync 容器，`sidecar` 在照本 repo 部署的叢集上只會 fail；`repo` 驗 `--path` 用的 `git archive --remote` GitHub 不支援，路徑從沒被遠端驗過。`--ci` 自 #3210 起已無作用（沒有子命令會給 warn）。帶 `repo`、`sidecar` 或 `--ci` 的呼叫結束碼 `0`／`1` → `2`；`local --dir` 照舊。
