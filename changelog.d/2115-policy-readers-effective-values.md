---
section: Changed
topic: confd-family
issues: [2115]
created: 2026-10-06T10:04:09+00:00
---
- **`evaluate-policy`、`validate-config` 的 Policy-as-Code 列、`opa-evaluate` 改讀生效值（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）**：⚠️ 行為變更：policy 開始對繼承值響。閾值看 `/metrics` 實際發出的數字（經 `da-guard served-values`），值寫在 `defaults:`、平台 `tenants:`、子目錄都算，子目錄租戶也評估；排程閾值每個時段都要過；舊拼法 target 也比得到。保留鍵看寫法＋繼承（`da-guard effective`，只收租戶可寫的鍵），exporter 自動補的不算有寫；`_metadata` 取 `/metrics` 的值、去掉空欄位；`_routing` 看路由產生器的解析結果。`opa-evaluate` 的 `input.tenants` 同此取法，另加 `input.served`。沒有 `tenants:` 的檔不再當租戶（印 `WARN`）；exporter 丟掉或讀不到的檔、被拒收的樹、沒有任何設定檔的目錄（含空目錄配 `--policy`）、找不到 da-guard 時 exit 2（`validate-config` 為該列 FAIL）。已知限制：根 `defaults:` 與租戶同時寫 `X_critical` 的樹會被拒收、不評估。
