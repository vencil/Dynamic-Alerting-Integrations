---
section: Changed
topic: da-tools
issues: [3142, 3208]
created: 2026-10-22T16:00:00+00:00
---
- **⚠️ 結束碼變更：部署端 `--policy` 改由同一個 resolver 讀，`test-notification`／`explain-route` 新增 `--policy`（[#3142](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3142)、[#3208](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3208)）**：`generate-routes`、`validate-config`、`test-notification`、`explain-route` 的 `--policy` 都經路由產生器的同一個讀取函式（含 `min_generator_version` 檢查）。`test-notification --policy` 不送出 `allowed_domains` 拒收的 receiver，標 `POLICY_REFUSED`、算失敗（帶 `--ci` 時 rc 1）；`explain-route --policy` 列出與追蹤的是產生器在這份 policy 下產出的路由，被拒收的 receiver 列在 `policy_refused`。兩者的 `--json` 加 `policy_applied`；沒帶 `--policy` 時 stderr 印一行 NOTICE、結束碼不變；policy 檔不可用或 `min_generator_version` 比工具新時 rc 2（`reason: policy_unusable`）。`validate-config` 先前不讀 `min_generator_version`，工具比它舊時照樣判定（rc 0），現在 routes、policy 兩列都是呼叫端錯誤（rc 0 → 2）。`validate-config` 的 `policy` 列 FAIL 時，建議修法改指向 `--policy` 檔的 `allowed_domains`，不再叫人去改 `_domain_policy.yaml`。
