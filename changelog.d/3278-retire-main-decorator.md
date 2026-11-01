---
section: Removed
topic: da-tools
issues: [3278]
created: 2026-11-01T16:55:09+00:00
---
- **移除 `_lib_exitcodes.main` decorator 與 `exit_on_crash(on_crash=)`（da-tools；[#3278](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3278)）**：正式工具沒有使用；da-tools 指令一律經 `run(main)` 進出，`--json` 的 crash envelope 由 `arm_crash_envelope` 提供。**Breaking**（僅限自行 import 這個模組的程式）。結束碼不變。
