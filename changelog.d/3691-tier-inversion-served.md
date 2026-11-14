---
section: Added
topic: tools
issues: [3691]
created: 2026-11-12T23:59:03+00:00
---
- **validate-config 新增 `tier_inversion` 列：判每個租戶實際拿到的 critical 是否比 warning 嚴（validate-config；[#3691](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3691)）**：⚠️ 有倒置時 FAIL。值取自 `da-guard served-values --schedules-only`（[#3877](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3877)：不讀單一時刻，幾點跑都同一個結論），逐租戶、逐時段比對，所以 profile 的 `<base>_critical` 對平台檔的 `<base>`、某個時段對另一層的預設值這類跨檔倒置也判得到（`check_threshold_unit_sanity` 只看單一檔案）。方向取自出貨告警的比較（`metric_observed_map.yaml` 的 `direction`），只判 warning 與 critical 兩層都有出貨告警在比的鍵；只正規化 warning 層的鍵、對照延後判的鍵，以及沒有租戶綁定的 profile，不判。exporter 丟掉的檔或讀數取不到時為 WARN「not checked」，`/metrics` 無法 gather 的時段會寫明未判。
