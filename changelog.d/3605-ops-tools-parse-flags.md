---
section: Fixed
topic: dx
issues: [3605]
created: 2026-11-13T06:50:58+00:00
---
- **4 支 `scripts/ops` 工具改為解析旗標（dx；[#3605](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3605)）**：`add_t_parallel`、`route_metrics_through_field`、`use_get_metrics`、`ci_flake_retry` 之前把 `--help` 等旗標當成路徑或要執行的命令而 crash。現在 `--help` 以 `0` 印用法；未知旗標是 argparse 點名的 `2`；三支 codemod 在改寫第一個檔之前判定全部路徑，不存在就點名 `2`、什麼都不寫（之前會先改掉前面的檔再 crash）；`ci_flake_retry` 的 `--` 仍可省（選項讀到第一個非選項為止，同 GNU `timeout`），命令跑不起來時點名 `2`。結束碼：`--help` `2`→`0`，其餘由 crash 的 `2` 變成具名的 `2`。
