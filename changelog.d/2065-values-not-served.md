---
section: Changed
topic: exporter
issues: [2065]
created: 2026-10-08T23:30:04+00:00
---
- **寫壞而被 `/metrics` 退回的閾值值改為出聲：exporter 載入時告警、da-guard 與 `validate-config` 擋下（threshold-exporter / da-guard / validate-config；[#2065](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2065)）**：值不是數字（`abc`、list、`7O:critical`，改送平台預設值或該 series 不送）、排程時段不被接受（`window_invalid`）、子目錄 `_defaults.yaml` 的值不是閾值形狀（`value_rejected`）時，exporter 每次載入與 reload 設定新 gauge `da_config_values_not_served{reason}`，並在集合變化時印一行 WARN 點名租戶、檔案、key 與 reason；判定一律取自 exporter 自己的 resolver／build 記錄（與 `da-guard effective` 的 `not_served` 同源），`/metrics` 送出的值不變。⚠️ 行為變更：da-guard 新增 error `value_not_served`，有這類值的既有樹由 exit 0 改為 exit 1；`validate-config` 新增 `values_not_served` FAIL 列（da-guard 的 `effective` 輸出缺 `not_served` 時視為 da-guard 太舊）；起訖相同的時段（如 `05:00-05:00`）原本是合法的空時段，現在視為不合法，exporter 每次解析都印 WARN。
