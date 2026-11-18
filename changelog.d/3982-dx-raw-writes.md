---
section: Changed
topic: da-tools
issues: [3982]
created: 2026-11-18T06:00:00+00:00
---
- **八支 dx／lint 工具的輸出旗標改經共用的輸出入口（[#3982](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3982)）**：`describe_tenant.py -o`、`migrate_conf_d.py -o`、`pair_bench_ratio.py --out`、`paired_trend_watch.py --summary-file`、`render_soak_diff.py --output`、`scan_component_health.py --output`、`trufflehog_to_sarif.py --output` 與 `run_chaos_soak.py --output-dir`。內容位元組不變；寫出的檔案（含自己擁有的既有檔、`--summary-file` 附加的檔）改為 `0600`，經 symlink 寫入的目標不改權限。stdout 另有用途的六支拒收 `-`（結束碼 `2`，argparse 點名旗標），以前會建出名為 `-` 的檔案或目錄；`describe_tenant.py -o -` 與 `render_soak_diff.py --output -` 改為寫到 stdout。空值一律結束碼 `2`：`describe_tenant`、`migrate_conf_d`、`render_soak_diff` 以前忽略它、結束碼 `0`，`run_chaos_soak --output-dir ""` 以前寫進目前目錄。沒有讀端的 FIFO 由卡住改為一行 `cannot write` 與結束碼 `2`。
