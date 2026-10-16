---
section: Fixed
topic: da-tools
issues: [3065]
created: 2026-10-16T08:20:02+00:00
---
- **`alert-quality` 查詢 Prometheus 失敗時改為結束碼 2，`cardinality-forecast` 分得出查詢失敗與沒有資料（tools；[#3065](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3065)）**：⚠️ 破壞相容——`alert-quality` 的 `ALERTS` 查詢失敗（連不上、HTTP 非 2xx、`status` 非 success、回應無法解析——HTTP 200 但非 JSON 或非物件，例如 proxy 回 HTML）以前被當成期間內沒有警報，報告全 GOOD、`--ci` 下 rc 0；現在不論有無 `--ci` 都是 rc 2，stderr 帶錯誤原文，`--json` 印 `status: caller_error`、`reason: prometheus_query_error` 與 `error` 原文。`cardinality-forecast` 查詢失敗原本就是 rc 2，但 `--json` 與沒有資料同為 `status: no_data`、`reason: no_cardinality_data`；⚠️ 現在查詢失敗的 `--json` 改為 `status: caller_error`（原 `no_data`）、`reason: prometheus_query_error`（原 `no_cardinality_data`）並帶 `error` 原文，stderr 也帶原文（`DA_LANG=zh` 時為中文）；查詢成功但沒有資料仍是 `no_data` / `no_cardinality_data`。
