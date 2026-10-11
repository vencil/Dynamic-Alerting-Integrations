---
section: Changed
topic: tenant-api
issues: [2856]
created: 2026-10-11T02:00:00+00:00
---
- **BREAKING — tenant-api 自己的紀錄搬出 conf.d，改存 state store（[#2856](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2856)）**：群組、views、federation 白名單與子集、AccountID 帳本過去只 commit 在 pod 本機、從不 push，emptyDir 重啟即失。現存於 ConfigMap `tenant-api-state`（chart 預建，Role 只給 get/update）或單機目錄。必填 `--state-store`（`configmap`／`dir`），未設不啟動；SA token 一律掛載，開了 `networkPolicy.egress` 須在 `extraEgress` 放行 K8s API。寫入走 resourceVersion compare-and-swap；滿了回 507 `STATE_FULL`。每筆寫入記 `state_write` 稽核 log，不再有 git 歷史；帳本的單調性只剩「id 不得 ≥ 計數器」檢查，直接改 ConfigMap 可能讓已退租的 id 重發。首次啟動把 conf.d 的舊檔匯入（不覆蓋既有紀錄；帳本取 git 歷史最大值；有租戶時空白帳本不匯入、照舊拒絕啟動），之後可刪。子集 key 為 `_federation.<tenant>.yaml`。
