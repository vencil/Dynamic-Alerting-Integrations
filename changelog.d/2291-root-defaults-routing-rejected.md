---
section: Fixed
topic: da-tools
issues: [2291, 1414]
created: 2026-09-28T10:05:40+00:00
---
- **`validate-config` 依 exporter 的解法檢查根目錄 `_defaults.yaml` 的 `defaults:`（da-tools；[#2291](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2291)、[#1414](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1414)）**：新增 `root_defaults` 檢查列，以下情形 FAIL（exit 1），先前全部檢查都回 PASS。exporter 把根目錄 `defaults:` 當純數值解：解不成數字的值（`"70"`、`disable`、mapping 等）會讓它丟掉整個區塊——所有平台閾值一起失效、載入仍回報成功；空值（`k:`、`~`）則解成 0，對每個沒有自訂值的租戶送出 0 閾值（只宣告 key 請改用 `optional_overrides:`）。另外，`defaults:` 底下出現 `_routing` 或任何 `_routing` 前綴的鍵一律 FAIL，並引導改寫成頂層的 `_routing_defaults:`——路由產生器從不讀 `defaults:`。子目錄的 `_defaults.yaml` 不在判定範圍。
