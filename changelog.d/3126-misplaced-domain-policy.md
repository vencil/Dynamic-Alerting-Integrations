---
section: Changed
topic: alertmanager-routing
issues: [3126]
created: 2026-10-22T15:59:08+00:00
---
- **⚠️ Breaking：`domain_policies:` 寫在 `_domain_policy.yaml` 以外的檔，路由產生器所有模式拒收整棵樹（alertmanager-routing；[#3126](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3126)）**：寫在 `_defaults.yaml`、租戶檔或其他 `_` 開頭檔（任何深度）的 `domain_policies:` 不會被讀，現在報 `domain_policy_unusable`（field `domain_policies`），render、`--dry-run`、`--output-configmap`、`--apply`、`--validate`、`--strict` 都回 1、不寫出不套用，訊息點名該檔並指出要搬到同目錄的 `_domain_policy.yaml`；空的 block 也算。過去只有 `--strict` 擋，非 strict 只印一行 WARN、結束碼 0，policy 靜默不生效。da-guard 把這種頂層鍵列為未判定證據 `domain_policy`（exit 4），不再回 0。
