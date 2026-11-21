---
section: Changed
topic: ci
issues: [3756]
created: 2026-11-21T16:00:00+00:00
---
- **Threshold Backtest 在更多 PR 上執行，沒跑成時不再留言（ci；[#3756](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3756)）**：`backtest.yaml` 的 `on.pull_request.paths` 改為涵蓋 job 實際讀的檔，所以只改 da-guard 的 Go 原始碼、工具 import 的共用 `_lib_*` 模組或 `Makefile` 的 PR 現在也會執行它（先前只有改 `conf.d/` 或工具本身才會）。結果一律寫進 job summary；因 Prometheus 不可達而略過（`status: skipped`、`reason: prometheus_unavailable`）時不再貼 PR 留言，其餘結果照舊留言。⚠️ job 不再整支 `continue-on-error`：安裝依賴或建置 da-guard 失敗會讓這支 check 顯示紅燈；backtest 本身與留言步驟的失敗仍不算。
