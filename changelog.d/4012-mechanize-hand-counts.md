---
section: Changed
topic: tools
issues: [4012]
created: 2026-11-19T03:30:00+00:00
---
- **原本靠手抄維持的告警數與清單，改成從生成資料推導或由守衛把關（[#4012](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4012)）**：
  - `platform-data.json` 的 `totals` 新增 `tenantAlertRules`：platform pack 以外的告警數，由 `generate_platform_data.py` 依 `PLATFORM_PACKS` 計算，也寫進離線備援檔。portal 引導精靈的告警數原本是手寫的 `ALERT_COUNT`，現在讀這個值；它應等於 `ALERT-REFERENCE.md` 的列數，由 portal 測試釘住。
  - 新增守衛 `tests/rulepacks/test_tenant_job_literal.py`：租戶 rule pack 的每個 `job` matcher 必須是 `job="tenant-exporters"`（`TENANT_EXPORTER_JOB`）；platform pack 的每個 `job` matcher 必須用 `=` 指向 repo 實際會產生的 job（Operator 安裝的 ServiceMonitor job，加上 Prometheus 設定裡固定 job 名的 scrape job）。值的三種引號寫法，以及 Prometheus 3 允許的帶引號 label 名稱（`{"job"="…"}`），都會檢查。
  - onboarding checklist 的 Domain Expert 與 Tenant 清單各新增一項：列出有「平台只宣告、不給值」閾值的 pack，提醒這些閾值不設就不會響、也不報錯，並說明怎麼訂閱 `_state_unset_thresholds` 提醒（既有部署要先請平台在 `state_filters` 加 `unset_thresholds`）。pack 名單由 registry 生成的 declared keys 決定，渲染測試會擋下手寫的名單。

  以上都不影響告警行為。
