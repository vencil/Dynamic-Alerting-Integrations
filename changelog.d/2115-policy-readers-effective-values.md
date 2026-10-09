---
section: Changed
topic: confd-family
issues: [2115, 2725]
created: 2026-10-06T10:04:09+00:00
---
- **`evaluate-policy`、`validate-config` 的 Policy-as-Code 列、`opa-evaluate` 改讀生效值（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）**：⚠️ 行為變更：policy 開始對繼承值響。閾值看 `/metrics` 實際發出的數字（經 `da-guard served-values`），值寫在 `defaults:`、平台 `tenants:`、子目錄都算，子目錄租戶也評估；排程閾值每個時段都要過；舊拼法 target 也比得到。保留鍵看寫法＋繼承（`da-guard effective`），只評估 exporter 認得的租戶保留鍵（其他 `_` 開頭的鍵不在 policy 的視野內），exporter 自動補的不算有寫；`_metadata` 取 `/metrics` 的值、去掉空欄位；`_routing` 看路由產生器的解析結果。`opa-evaluate` 的 `input.tenants` 同此取法，另加 `input.served`；已知限制：其中的 `_routing` 是寫法＋繼承，不是路由產生器的解析結果。沒有 `tenants:` 的檔不再當租戶（印 `WARN`）；exporter 丟掉或讀不到的檔、被拒收的樹、沒有任何設定檔的目錄（含空目錄配 `--policy`）、找不到 da-guard、da-guard 本身跑不起來或太舊時 exit 2（`validate-config` 為該列 FAIL；樹裡另有讀不到的檔時為 1）。`validate-config` 該列在 `/metrics` 一個閾值都沒送、或有設定了卻沒送出的閾值值時於明細點名。已知限制：根 `defaults:` 與租戶同時寫 `X_critical` 的樹會被拒收、不評估。
