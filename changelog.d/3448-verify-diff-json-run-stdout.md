---
section: Fixed
topic: da-tools
issues: [3448]
created: 2026-11-01T16:49:44+00:00
---
- **verify-diff `--json --run` 時 pytest 的輸出不再混進 stdout（dx；[#3448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3448)）**：pytest 子行程的輸出原本跟文件寫進同一個 stdout，整份無法解析；現在 `--json` 時改導到 stderr（stderr 已關閉時丟棄）。沒有 `--json` 時不變。結束碼不變。
