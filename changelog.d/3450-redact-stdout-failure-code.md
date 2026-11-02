---
section: Fixed
topic: da-tools
issues: [3450]
created: 2026-11-01T16:44:31+00:00
---
- **waveform-score `--json` 遇 stdout 寫入失敗時仍印 `ERR_OUTPUT`（da-tools；[#3450](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3450)）**：`--json` 文件改由 `run()` 寫出後，stdout 寫入失敗或讀端離開時只印共用的 `ERROR: cannot write to stdout: …`／`ERROR: stdout was closed …`，`--redact` 文件化的去識別化錯誤碼因此遺失。現在 `Report` 可帶 `on_stdout_failure` 指定這一行，waveform-score 在兩種模式都印 `ERROR [ERR_OUTPUT]: …`，`--redact` 下不附細節。結束碼不變（2）。
