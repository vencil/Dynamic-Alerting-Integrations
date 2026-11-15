---
section: Changed
topic: confd-family
issues: [3902]
created: 2026-11-15T10:30:00+08:00
---
- **⚠️ Breaking：`deprecate` 以 `--subtree` 取代 `--plane`／`--root-dir`；`backtest`、`opa-evaluate`、`offboard` 的 conf.d 輸入改為宣告、拒收時點名旗標（tools；[#3902](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3902)）**：`deprecate` 的 `--config-dir` 一律給 conf.d 的 root，子樹改用相對路徑 `--subtree <DIR>`；舊寫法 `--config-dir <root>/team --plane subtree --root-dir <root>` 改成 `--config-dir <root> --subtree team`（舊旗標是用法錯誤，結束碼 2）。子樹模式下，載體在判定後消失不再被當成「本工具讀不了」繼續跑（結束碼 1），而是結束碼 2、點名 `--config-dir`。`backtest --config-dir`／`--baseline`、`opa-evaluate --config-dir`、`offboard --config-dir` 不是可用的目錄、（前兩支）沒有任何 config 檔，或判定後被改動時，結束碼 2、一行點名旗標與路徑；`backtest` 的未執行報告 `reason` 由 `served_values_unavailable` 改為 `dir_not_found`／`dir_not_accessible`／`input_empty`／`input_changed`。`offboard --config-dir ''` 以前被當成目前目錄（結束碼 1），現在拒收。
