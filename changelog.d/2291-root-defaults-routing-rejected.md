---
section: Fixed
topic: da-tools
issues: [2291]
created: 2026-09-28T10:05:40+00:00
---
- **`validate-config` 擋下根目錄 `_defaults.yaml` 的 `defaults:` 底下的 `_routing`（da-tools；[#2291](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2291)）**：新增 `root_defaults` 檢查列，`defaults:` 底下出現 `_routing` 或任何 `_routing` 前綴的鍵即 FAIL（exit 1），並引導改寫成頂層的 `_routing_defaults:`。這個寫法先前全部檢查都回 PASS，但 exporter 把根目錄 `defaults:` 當純數值解，遇到 `_routing` mapping 會丟掉整個區塊——所有平台閾值一起失效、載入仍回報成功——而路由產生器也從不讀 `defaults:`。子目錄的 `_defaults.yaml` 不在判定範圍。
