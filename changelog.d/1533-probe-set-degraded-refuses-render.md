---
section: Changed
topic: alertmanager-routing
issues: [1533]
created: 2026-10-10T12:39:59+08:00
---
- **平台告警探針集降級時，`generate-routes` 不再放行驗證不了的租戶觸發 inhibit 規則（alertmanager-routing；[#1533](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1533)）**：行為變更：原本 `configmap-rules-platform.yaml` 找不到、讀不了或讀不出平台告警時，探針集退回內建 6 筆、只印一行 WARN，`--output-configmap` 與 `--apply` 照樣以結束碼 0 寫出／套用，即使 base config 或叢集設定裡有租戶觸發、會靜音 6 筆以外平台告警的 inhibit 規則。現在只要探針集降級、而你提供的 inhibit 規則含有租戶觸發的規則，就以結束碼 2 拒絕、不寫檔也不 apply，訊息點名降級原因與出問題的 `inhibit_rules[<i>]`；沒有這種規則、或規則的 target 已排除平台告警（如 `alert_source=""`）時照舊只印 WARN。`--validate` 結束碼不變，stdout 多印一行說明探針集已降級。
