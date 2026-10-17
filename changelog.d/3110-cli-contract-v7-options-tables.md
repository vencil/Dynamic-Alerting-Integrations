---
section: Fixed
topic: docs
issues: [3110]
created: 2026-10-17T07:48:29+00:00
---
- **cli-reference 選項表：程式有的旗標都要列出來（[#3110](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3110)）**：`check_cli_contract` 新增 V7，命令宣告了某個選項（含短旗標、子命令的選項）而該命令章節的選項表沒有這一列時會擋下；只寫在散文或範例裡不算，`help=SUPPRESS` 的隱藏旗標不要求。中英兩份補齊 19 個命令漏列的旗標，config-history、check-alert、gitops-check 補上選項表；backtest 的 `-o/--output` 更正為「另把 JSON 報告寫到檔案」（從來沒有 CSV 輸出），並補上 `--tenant`、`--markdown-output`、`--skip-if-unavailable` 等旗標。
