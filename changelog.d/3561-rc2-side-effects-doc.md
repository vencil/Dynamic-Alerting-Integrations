---
section: Changed
topic: docs
issues: [3561]
created: 2026-11-13T06:51:29+00:00
---
- **cli-reference 註明「stdout 寫不進去」的 `2` 可能已發生副作用（docs；[#3561](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3561)）**：`deprecate-rule --execute`、`maintenance-scheduler` 這類命令做完事之後才寫 stdout，stdout 寫不進去時以 `2` 結束，但檔案或 silence 可能已經改了。分派層結束碼表那一列補上這點與確認方式；結束碼不變。
