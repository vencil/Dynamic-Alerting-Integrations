---
section: Removed
topic: threshold-governance
issues: [2996]
created: 2026-10-16T02:40:49+00:00
---
- **`threshold-recommend`：移除從未接上的 `noise_grade` 參數與「整合 Noise Score」的說法（[#2996](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2996)）**：`recommend_threshold()` 的 `noise_grade` 參數（BAD 時改用 P99）自 v2.1.0 引入起，唯一的呼叫端就從未傳入，CLI 與 `threshold-govern` 也沒有對應旗標，所以 CLI 參考、模組說明與 DB2／Oracle rule pack 註解所稱的「結合 alert_quality Noise Score 調整推薦方向」實際上不存在。現已刪除該參數與其分支，並拿掉這些說法。CLI、JSON 輸出、`--export-patch` 與 govern 的行為不變；只有在 repo 外直接 import `recommend_threshold()` 並傳 `noise_grade` 的程式會收到 `TypeError`。
