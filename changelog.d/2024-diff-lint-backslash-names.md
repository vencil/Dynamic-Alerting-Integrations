---
section: Fixed
topic: dx
issues: [2024]
created: 2026-09-27T04:55:24+00:00
---
- **diff 掃描不再把檔名裡的反斜線當成路徑分隔符（lint；[#2024](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2024)）**：`check_ad_hoc_git_scripts` 與 `check_repo_name` 的 diff 模式改為只以 `/` 拆分 git 列出的路徑。先前根目錄的 `build\evil.bat` 會被當成 `build/` 下的檔而略過、`scripts\ops\_x.bat` 會被當成 allowlist 目錄下的檔而放行，違規因此不報。
