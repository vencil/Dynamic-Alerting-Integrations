---
section: Fixed
topic: dx
issues: [2196]
created: 2026-09-27T17:40:00+00:00
---
- **Windows 腳本 lint 的副檔名比對改為不分大小寫（lint；[#2196](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2196)）**：`check_ad_hoc_git_scripts` 與 `check_bat_ascii_purity` 的 diff 模式、`--full-scan` 與 pre-commit `files:` 過濾原本都分大小寫，`evil.BAT` 三層都被放行；Windows 執行 `FOO.BAT` 與 `foo.bat` 沒有差別，現在一併擋下。`foo.batx` 這類只是字首相同的檔名仍不在範圍內。
