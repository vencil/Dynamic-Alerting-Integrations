---
section: Fixed
topic: tenant-api
issues: [2855]
created: 2026-10-10T23:26:17+00:00
---
- **BREAKING — tenant-api chart 必須設定寫回模式，PR 模式可以從 chart 啟用（helm；[#2855](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2855)）**：chart 過去不傳 `--write-mode`、也沒有任何 env 出口，所以用 chart 部署一律跑 `direct`：寫入只 commit 在 pod 自己的 `/conf.d`，不 push，exporter 讀的是由設定 repo 組出的 `threshold-config` ConfigMap，因此經 tenant-api 寫入的設定送不到 exporter；`/conf.d` 預設是 emptyDir，pod 重啟後寫入全部消失。現在 `writeBack.mode` 沒有預設值，不設 chart 就不 render。`pr-github` / `pr-gitlab` 每筆寫入開一個 PR/MR，需要 `gitRepoUrl`、`writeBack.github.repo`（或 `writeBack.gitlab.project`）與放 forge token 的 Secret（`writeBack.token.secretName`）；同一個 token 也用於 `git clone` / `git push` 的 HTTPS 認證，以環境變數交給 git 的 credential helper，不寫進 URL、ConfigMap 或磁碟。`direct` 仍可用，但要另設 `writeBack.directAcknowledgeNotDelivered=true` 表示知道寫入不會送到 exporter。升級時：既有 values 加上 `writeBack` 區塊；先前照文件設的 `tenantApi.env` / `tenantApi.secretEnv` chart 從未讀過，可以刪掉。portal 部署精靈產生的 tenant-api values 同步改為帶 `writeBack`，`replicaCount` 一律為 1（過去 staging / production 產生 2，chart 本來就拒收）。
