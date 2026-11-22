---
section: Added
topic: rule-packs
issues: [3771, 4097]
created: 2026-11-13T10:31:00+00:00
---
- **新告警 `TenantExporterUndeclared`：有某種 exporter 在回應、卻沒在 `_expected_exporters` 宣告那一種（rule-packs；[#3771](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3771)）**：exporter 存活告警只涵蓋租戶在 `_expected_exporters` 宣告了的種類。這條規則（liveness pack，`severity: warning`、`maintenance: mute`）逐（租戶, `pack`）判斷：1 小時內在 `job="tenant-exporters"` 下出現過某一種 exporter 的存在指標（`mysql_up`、`redis_up`、`nginx_up` 等）、而租戶沒宣告那一種，持續 15 分鐘即響——整個租戶沒宣告，或宣告了 `[mariadb]` 卻同時跑著 redis exporter 都算。`jvm` 不點名（很多應用順帶吐 `jvm_*`；要納管就自己宣告），只有應用自己 /metrics 的租戶也不響；exporter 全掛時 1 小時內不會自己解除；threshold-exporter 自己掛掉或租戶已退租時不響。**升級須知**：既有租戶有沒宣告的 exporter 時升級後會收到這條 warning，把該種類補進 `_expected_exporters` 即可；`scaffold_tenant` 不寫這個鍵，新建租戶需手動補。
