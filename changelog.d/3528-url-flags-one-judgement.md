---
section: Changed
topic: da-tools
issues: [3528]
created: 2026-11-07T07:44:16+00:00
---
- **⚠️ Breaking：URL 旗標在解析參數時以同一個判定擋下；`da-tools` 不再補 `--prometheus`（da-tools；[#3528](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3528)）**：`--prometheus`、`--alertmanager`、`--pushgateway`、`--endpoint`、`--opa-url`、`--edge-urls` 等的值不是 http(s)、沒有 host、埠不是數字、含空白或控制字元、路徑或查詢字串含未編碼的非 ASCII 字元，或為空字串時，一律 rc 2，argparse 點名旗標（ms 的 `--pushgateway ""`／`--alertmanager ""` 仍等於沒給）。rc 變化：ms `--pushgateway`、`--alertmanager` 壞值 0→2；`--endpoint ""`、discover `--prometheus ""` 1→2；alert-correlate／blind-spot `--prometheus ""`、shadow-verify `--alertmanager ""` 0→2（原本當成沒給）；threshold-govern／recommend 壞值 0→2；reconciler 壞 `--victorialogs-url` 由不停重試改為 2。`$PROMETHEUS_URL` 改由工具在會查詢的執行開始處才讀、才判定（不合格 rc 2，dry-run 不受影響；operator-check、runtime-audit 不讀它）。alert-quality、cardinality-forecast 的 `--prometheus` 改為可省略（預設 `$PROMETHEUS_URL`，否則 localhost:9090）。
