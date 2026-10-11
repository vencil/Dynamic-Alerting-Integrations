---
section: Fixed
topic: tenant-api
issues: [2855]
created: 2026-10-10T23:26:17+00:00
---
- **BREAKING — tenant-api chart 必須設定寫回模式，PR 模式可從 chart 啟用（helm；[#2855](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2855)）**：chart 過去不傳 `--write-mode`，chart 部署一律跑 `direct`：寫入只 commit 在 pod 的 `/conf.d`、不 push，送不到 exporter（它讀由設定 repo 組出的 `threshold-config` ConfigMap），emptyDir 預設下重啟即失。現在 `writeBack.mode` 沒有預設值，不設就不 render。`pr-github`／`pr-gitlab` 需要 `https://`、不帶帳密的 `gitRepoUrl`、`writeBack.github.repo`（或 `gitlab.project`）與放 forge token 的 Secret（`writeBack.token.secretName`）；同一個 token 經 git credential helper 認證 clone 與 push，只交給 `gitRepoUrl` 那個 host，不寫進 URL、ConfigMap 或磁碟。`direct` 要另設 `writeBack.directAcknowledgeNotDelivered=true`（布林值，字串不算）。升級時在 values 加上 `writeBack`；舊文件的 `tenantApi.env`／`secretEnv` chart 從未讀過，可刪。portal 部署精靈產出的 tenant-api values 同步帶 `writeBack`，`replicaCount` 一律為 1。
