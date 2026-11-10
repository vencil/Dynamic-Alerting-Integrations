---
section: Changed
topic: tenant-api
issues: [1018]
created: 2026-11-07T15:00:00+00:00
---
- **⚠️ Breaking：tenant-api chart 移除 `confDir.type=hostPath`，新增 `confDir.pvc.existingClaim`（tenant-api chart 2.9.25；[#1018](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1018)）**：hostPath 是唯一連 Pod Security baseline 都過不了的 chart 選項，而且 chart 以非 root 執行、fsGroup 不作用於主機目錄，預設設定下跑不起來。仍設定它的 release 會在 render 時失敗並列出遷移步驟：把節點上的 repo 放進 PV（目錄擁有者須是 `podSecurityContext.runAsUser`），綁一個 PVC，再設 `confDir.type=pvc` 與 `confDir.pvc.existingClaim`；或改設 `gitRepoUrl` 讓 chart clone。`values.schema.json` 也開始檢查 `confDir`：`type` 只收 `emptyDir`／`pvc`，未知鍵會被拒，`confDir.type=PVC` 這類錯字不再悄悄變成 emptyDir。
