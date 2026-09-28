---
section: Fixed
topic: dx
issues: [1920]
created: 2026-09-28T15:27:25+00:00
---
- **Windows 逃生門不再把打錯的子命令當成功（dx；[#1920](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1920)）**：`win_git_escape.bat` 遇到不存在的子命令（例如文件曾教的 `raw`）印 usage 後改回 rc 1，與 `win_gh.bat` 一致。pre-push 的 preflight 攔截橫幅多印一行不需要 `make` 的等價指令，給 Windows host 用。
