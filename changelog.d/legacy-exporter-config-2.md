---
section: Fixed
topic: exporter-config
issues: [1447, 1497, 1521, 1526, 1568, 1911, 1964, 1969, 1972, 2028]
created: 2026-09-26T17:00:00+00:00
---
- **巢狀 conf.d 租戶終於產生指標，熱重載的靜默失效一併修正（threshold-exporter、helm）**：宣告在 `conf.d/` 子目錄的租戶原本 `/effective` 查得到、`/metrics` 卻沒有任何 `user_threshold` 且零訊號（告警永遠不觸發）；現在會產生指標並繼承子樹 defaults，副檔名不分大小寫，`-config-dir` 為 symlink 亦可載入。送不上輸出面的子樹 key 改以 ERROR 逐一列出租戶與 key 並計入 gauge。熱重載：ConfigMap（symlink）換版後會生效、defaults chain 成員變動會重算 merged_hash、`_profiles.yaml` 與 profile 閾值不再於增量重載後遺失、同次重載新增加修改多檔不再停在舊值、從仍存在的檔案移除租戶會真的移除、樹變成空的時保留前一份 config 並報錯而非清空。⚠️ `max_metrics_per_tenant` 在目錄模式（Helm）終於生效、只採信根 `_defaults.yaml`，chart 新增 `thresholdConfig.max_metrics_per_tenant`；曾在根 `_defaults.yaml` 寫過它的部署上限會改變。詳 [#1521](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1521)、[#1969](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1969)、[#2028](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2028)。
