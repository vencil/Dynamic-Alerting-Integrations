---
section: Changed
topic: tenant-api-rbac-identity
issues: [1040]
created: 2026-11-05T16:30:00+00:00
---
- **⚠️ Breaking：tenant-api 的 API 只在 Unix socket 上、經同一個 pod 的 oauth2-proxy（tenant-api、helm、try-local；[ADR-027](docs/adr/027-tenant-api-caller-identity.md) §2、§3；[#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)）**：`--human-socket`／`TA_HUMAN_SOCKET` 與 `--oauth2-proxy-config`／`TA_OAUTH2_PROXY_CONFIG` 改為必填，缺一即拒絕啟動。TCP port（8080）只有 `/health`、`/ready`、`/metrics`，其餘 404，帶身分 header 一律 400——仍經 8080 呼叫 API 的元件要改走 oauth2-proxy；本 repo 的 recipe-preview 與 da-portal（Tier-2）chart 仍走 8080，要等它們移進 tenant-api 的 pod 才恢復，所以在那之前不得發布（ADR-027 §6）。rate limit、request log 與開發旁路改讀身分物件，email／group header 名照設定檔、不再限 `X-Forwarded-*`。socket 路徑由 chart 固定；chart 刪除 `humanSocket`、`machineIdentity`（連同 TokenReview 的 ClusterRole）與 NetworkPolicy 對 recipe-preview、da-portal 的 8080 放行，values 仍設定它們或 `oauth2Proxy.enabled=false` 即拒絕 render；tenant-api 的 `TA_MACHINE_IDENTITY_*` 與 `--machine-identity-*` 一併刪除，仍帶著的舊設定啟動即拒。try-local 與契約測試改在前面跑 oauth2-proxy（免登入）、身分由開發旁路注入；tenant-api 的 probe port 為 8083。
