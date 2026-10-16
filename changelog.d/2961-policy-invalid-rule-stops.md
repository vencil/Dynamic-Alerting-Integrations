---
section: Changed
topic: da-tools
issues: [2961, 2987]
created: 2026-10-14T01:21:56+00:00
---
- **policy 規則讀不懂就停，不再跳過（evaluate-policy／validate-config；[#2961](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2961)）**：規則帶不認得的鍵（例如 `check:`）、不認得的 `operator`／`severity`、缺 `target`、`exclude_tenants` 寫成單一字串、`when` 寫錯，或 `policies` 不是清單時，原本會跳過或改讀成 `required`，結果算通過；運算子與 `value` 不搭（比較運算子沒有 `value`、`required` 帶 `value`、`one_of` 不是清單、`matches` 的 regex 編不起來）、帶 `.` 的萬用字元 `target`（`_metadata.*`）、萬用字元 `when` 用取值運算子（主規則與 `when` 皆然），原本規則被跳過或恆成立，或每個租戶都報違規（[#2987](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2987)）。現在不評估任何規則並逐條點名：`evaluate-policy` 回 2（`reason: policy_rule_invalid`）；validate-config 的 `policy_dsl` 列，寫在 `_defaults.yaml` 的 FAIL、回 1，寫在 `--policy-dsl` 檔的回 2。`--policy`／`--policy-dsl` 檔出現 `policies:` 以外的頂層鍵（例如拼錯的 `polices:`）也回 2，先前算「沒有規則」回 0。⚠️ 已有這類規則的 CI 升級映像後會轉紅：照訊息改正規則。
