---
section: Added
topic: rule-packs
issues: [1200, 1175]
created: 2026-11-15T13:06:26+00:00
---
- **可訂閱的「沒設定閾值」提醒（rule pack 的 `<Pack>ThresholdNotConfigured`、exporter 的 `user_threshold_unset`；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200) D-A A3、[#1175](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1175)）**：新 state filter `unset_thresholds` 是開關，出貨預設 `default_state: disable`；平台改 `enable` 就通知所有租戶，租戶寫 `_state_unset_thresholds: enable`／`disable` 自選。開著的租戶，對平台不出貨值（registry `arming: operator`）、沒送出任何門檻值、也沒寫 `disable` 的 key，exporter 輸出 `user_threshold_unset{tenant,pack,key,fix}`；寫了但不會送出的值不算設定，`fix` 為 `tenant`（根層已宣告）或 `platform`（未宣告，要平台先宣告）。11 個 pack 各加 info 告警（例如 `RedisThresholdNotConfigured`），該種 exporter 有回應、非維護模式時 30 分鐘後觸發。寫 `disable` 結束提醒，key verdict 判為有效。既有部署的 `_defaults.yaml` 要自行加上這個 filter，租戶才訂閱得到。
