---
section: Security
topic: routing
issues: [3257, 1560]
created: 2026-10-21T23:59:00+00:00
---
- **⚠️ breaking：租戶 receiver 不再接受檔案型欄位（routing；[#3257](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3257)）**：receiver 任何一層（含 `http_config`）以 `_file`、`_ref` 結尾的鍵或 `files` 清單，會讓 Alertmanager 讀取它自己容器內的檔案，因此 schema 移除這些欄位，路由產生器在所有模式下整棵樹拒收（rc 1），tenant-api 寫入回 422。判斷是規則而非欄位清單。需要放 Secret 的憑證在 [#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560) 的 `secretRef` 接線前，由平台寫在 base config。`--base-config`／叢集設定的 `global` 若有 receiver 會繼承的 `smtp_auth_*` 或 `http_config` 憑證，產生器也拒收；Alertmanager Deployment 不再掛載 ServiceAccount token。
