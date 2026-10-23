---
section: Changed
topic: da-tools
issues: [3092]
created: 2026-10-20T13:17:30+00:00
---
- **測試下，經 crash 出口結束的例外不再是 rc 2（[#3092](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3092)）**：Python 工具與 da-tools entrypoint 在 crash 時讀環境變數 `DA_TEST_CRASH_EXIT_CODE`，值為 3..255 的整數時以它結束，優先於工具自設的 crash 碼；其他值一律忽略。測試套件以 70 設定它，讓只斷言 `returncode == 2` 的測試能分出「具名錯誤」與「crash」；工具自己以寬的 `except` 接住例外再回 `2` 的不在此列。正式環境不設，結束碼不變；具名的呼叫端錯誤與 stdout 已關的結束碼不受影響。
