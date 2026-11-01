---
section: Removed
topic: operator
issues: [3427]
created: 2026-11-01T01:31:51+00:00
---
- **⚠️ Breaking：移除 repo 提交的 `operator-manifests/`，PrometheusRule 一律按需產生（[#3427](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3427)）**：那 16 份 PrometheusRule 少了生成器早已輸出的 `release: kube-prometheus-stack` selector label，而副本閘門只比 rule 內容，所以一直綠。改用 `da-tools operator-generate --components rules --output-dir <你的目錄>` 產生到自己的目錄（Helm release 名不同時加 `--selector-label release=<名稱>`）。`check_rulepack_sync` 只比 `rule-packs/` 與 ConfigMap 兩份副本，生成器的輸出改由測試釘住：每個 pack 一份、內容等於來源、帶齊兩個 selector label。`migrate-to-operator` 與 `rule-pack-split` 產出的 PrometheusRule 也改帶同一組 label（同一份定義），以前只帶 `prometheus`。
