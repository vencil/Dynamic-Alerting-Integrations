---
section: Fixed
topic: exporter
issues: [2377]
created: 2026-09-28T16:44:19+00:00
---
- **壞值不再帶 severity（exporter；[#2377](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2377)）**：閾值寫成「無法解析的值:severity」（例如 `"7O:critical"`、`"abc:foo"`）時，exporter 會退回平台預設值，卻保留租戶寫的 severity，產生 `80/critical`、`80/foo` 這類值與 severity 不相干的 series；和 `<metric>_critical` 同時存在時還會出現兩條同 label 的 series，讓整個 `/metrics` 回 500。現在後綴隨壞值一起丟棄，結果與不帶後綴的 `"7O"` 相同（預設值／`warning`）。壞值本身該發出什麼訊號仍依 [#2065](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2065) 裁決。
