---
section: Fixed
topic: dx
issues: [2230]
created: 2026-09-28T01:18:40+00:00
---
- **`.gitattributes` 的 Windows 腳本換行規則改為不分大小寫（[#2230](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2230)）**：`*.bat`／`*.cmd`／`*.ps1` 只配小寫，`FOO.BAT`、`x.PS1` 會落到全域的 LF 規則，checkout 後 cmd.exe 解析錯誤；改用括號樣式後各種大小寫都是 CRLF，`foo.batx` 這類只是字首相同的檔名不受影響。
