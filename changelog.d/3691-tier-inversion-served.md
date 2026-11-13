---
section: Added
topic: tools
issues: [3691]
created: 2026-11-12T23:59:03+00:00
---
- **validate-config 新增 `tier_inversion` 列：判每個租戶實際拿到的 critical 是否比 warning 嚴（validate-config；[#3691](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3691)）**：⚠️ 有倒置時 FAIL。值取自 `da-guard served-values --schedules`，逐租戶、逐時段比對，所以 profile 的 `<base>_critical` 對平台檔的 `<base>`、某個時段對另一層的預設值這類跨檔倒置也判得到（`check_threshold_unit_sanity` 只看單一檔案）。方向取自出貨告警的比較（`metric_observed_map.yaml` 的 `direction`）；出貨告警不比較或對照延後判的鍵，以及沒有租戶綁定的 profile，不判。
