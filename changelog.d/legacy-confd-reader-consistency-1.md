---
section: Fixed
topic: confd-reader-consistency
issues: [1339, 1469, 1537, 1588, 1589, 1603, 1604, 1607, 1630, 1654, 1679, 1714, 1761, 1911, 2055]
created: 2026-09-26T17:00:00+00:00
---
- **conf.d 工具鏈與 exporter 對「哪個檔是租戶載體」給同一答案（tools、dx；[#1603](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1603)、[#1588](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1588)、[#1607](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1607)）**：exporter 收 `.yaml`／`.yml`、副檔名不分大小寫、跳過 `.` 開頭的檔與目錄，但多支 Python 工具各自手寫判定，對 `.yml`、`upper.YAML` 或隱藏檔靜默給出不同答案：自訂告警編譯器編不出該租戶的告警、`operator_generate` 少產 AlertmanagerConfig CRD、`gitops-check local` 回報就緒卻數到 0 個租戶檔、portal 租戶清單缺人、`drift_detect`／`config_history`／`backtest_threshold`／`run_chaos_soak`／`analyze-gaps` 漏看變更。現在統一走 `_lib_confd` 的共享述詞；讀不到的項目（目錄、斷鏈 symlink）改為具名警告而非靜默消失。`validate-config` 改為遞迴讀取階層式 conf.d，其餘刻意平面讀取的工具遇到子目錄時逐一列出被跳過的檔。
