---
section: Fixed
topic: alertmanager-routing
issues: [2758]
created: 2026-10-09T13:08:42+00:00
---
- **receiver type 約束的項目是集合時不再讓產生器崩潰（[#2758](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2758)）**：`_domain_policy.yaml` 的 `forbidden_receiver_types`／`allowed_receiver_types` 若有項目是 mapping、list 或 `!!set`（例如 `[slack, {a: 1}]`），`generate-routes` 與 `explain-route --trace` 先前直接 traceback、結束碼 1；`allowed` 混有 null 與字串項目時（`[!!null x, email]`），違規訊息也會 traceback。現在該項目不指名任何 type 而被略過，其餘項目照常執行，非空的 `allowed` 清單仍然限制；`--strict` 以具名 ERROR 點名該項目，da-guard 以 `domain_policy_unusable` 回報同一處，tenant-api 維持整份拒收。
