---
section: Changed
topic: custom-alerts
issues: [3229]
created: 2026-10-23T06:30:00+00:00
---
- **自訂告警規則包改由 Go 產生，recipe 規則只剩一份（custom-alerts；[#3229](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3229)）**：新增 `da-guard compile-custom-alerts --config-dir <dir>`，以 JSON 印出規則包——逐條判定、recipe_id 與 PromQL 都出自 exporter 送出門檻用的同一段 Go 程式；exporter 不送的條目列在 `not_compiled`，用到非 active recipe 的租戶列在 `lifecycle`。`compile_custom_alerts.py` 改為呼叫它，只負責 `--out`、`--check`、空結果不覆蓋與 YAML 序列化；committed 的 `rule-pack-custom-alerts.yaml` 重產後逐位元組相同。Python 的 `custom_alerts/`（`shape.py`、`recipes.py`、`entries.py`）整個刪除。recipe-preview 映像改帶 da-guard，預覽的 recipe 由 exporter 判定：exporter 拒收的 recipe（例如 `mode`、`name` 不合法）回 `error` 並附 exporter 的原因，不再預覽一條部署後不會送出的 recipe；`threshold: disable` 的 recipe 回 `error`（它不會觸發）；合成的 `user_threshold` 序列改用 exporter 實際送出的標籤與值。
