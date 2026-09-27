---
section: Fixed
topic: cli-docs-accuracy
issues: [1513]
created: 2026-09-27T10:40:00+00:00
---
- **分段採用與租戶生命週期的命令改成工具真的行為（docs；[#1513](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1513)）**：分段採用的 promotion checklist 不再教不存在的 `shadow-verify --window`／`--check-subset-overlap`，噪音改用 `alert-quality --period 14d` 判，subset overlap 註明沒有工具、要逐筆對照；租戶生命週期的下架步驟原本標「執行」的 `offboard` 沒帶 `--execute`（只會預檢、不會刪）也沒帶 `--config-dir`，`deprecate` 同樣沒帶 `--execute`，三者都補上；另外兩處照抄會 rc=2 的也修掉：觀察期那行 `alert-quality` 少了必填的 `--prometheus`，工具表的 `deprecate` 列少了 `--config-dir`。閘門最後一個指著活缺陷的探針（inline span）改以真 parser 判合成頁面。
