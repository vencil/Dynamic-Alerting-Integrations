---
section: Changed
topic: da-tools
issues: [3982]
created: 2026-11-18T06:00:00+00:00
---
- **八支 dx／lint 工具的輸出旗標改經共用的輸出入口（[#3982](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3982)）**：`describe_tenant -o`、`migrate_conf_d -o`、`pair_bench_ratio --out`、`paired_trend_watch --summary-file`（照舊附加）、`render_soak_diff --output`、`scan_component_health --output`、`trufflehog_to_sarif --output`、`run_chaos_soak --output-dir`。內容不變；自己擁有的一般檔寫完一律 `0600`（含既有檔與其硬連結），symlink 的既有目標與別人的檔不改權限。結束碼 `2`、argparse 點名旗標：stdout 另有用途的六支拒收 `-`（以前建出名為 `-` 的檔或目錄），五支單檔旗標並拒收 stdout 的別名（`--out F > F`）；要名為 `-` 的檔請傳 `./-`。空值一律拒收（以前三支忽略它、`run_chaos_soak` 寫進目前目錄）。`describe_tenant -o -`、`render_soak_diff --output -` 改寫到 stdout。沒有讀端的 FIFO 由卡住改為 `cannot write` 與結束碼 `2`；寫到一半失敗時檔案留空（以前留前半段）。
