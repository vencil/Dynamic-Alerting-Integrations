---
section: Fixed
topic: confd-family
issues: [2086]
created: 2026-09-27T07:38:19+08:00
---
- **Custom Alerts 編譯器不再讀 exporter 不讀的路徑（`compile_custom_alerts`；[#2086](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2086)）**：`compile_custom_alerts` 原本會讀 `.` 開頭的檔案與目錄，以及 ConfigMap 掛載的 `..<timestamp>/` payload——隱藏檔宣告的 recipe 被編成多出來的 rule，ConfigMap 版面的每個租戶被讀兩次、各誤報一筆 `duplicate custom-alert name`。現在 conf.d 的列舉與 exporter 的 walker 一致（剪掉隱藏目錄、略過隱藏檔）；讀不到的檔案照舊列在 skip 報告裡，讀不到的子目錄現在也會被點名（以前整棵子樹靜默消失），同租戶超過 cap 時保留哪一條 recipe 的順序不變。
