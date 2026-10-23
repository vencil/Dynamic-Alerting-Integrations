---
section: Changed
topic: exporter
issues: [3295]
created: 2026-10-23T14:00:00+08:00
---
- **⚠️ Breaking：子目錄 `_defaults.yaml` 帶時間窗的保留鍵，改為與同一層只寫 `default` 完全相同（exporter；[#3295](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3295)）**：overlay 先剝掉時段與 `expires:`，再判斷能不能往下傳；先前連時段裡的值一起判斷，而讀取端只讀 `default`。升級後送出結果會變的寫法有兩種：`_state_X: {default: disable 或數字, overrides: …}` 由整個不套用改為套用 `default`，出貨的 `container_crashloop`／`container_imagepull` 會因此對這些租戶關掉（與寫 `disable` 相同）；`_silent_mode`／`_severity_dedup` 的 `default` 本身不會往下傳（例如 `warning`）、只靠時段裡的數字通過判斷的寫法，由套用改為不套用（`_silent_mode` 原被靜音的告警恢復通知）。修法：刪掉時段，在該層寫成你要的純量。
