---
section: Changed
topic: confd-family
issues: [2544, 4173]
created: 2026-10-02T15:00:00+00:00
---
- **⚠️ breaking：根 `_defaults.yaml` 的 `<key>_critical` 成為每個租戶的 critical 預設值（exporter、da-guard、tenant-api；[#2544](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2544)、[#4173](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4173)）**：先前根 `defaults:` 的 `_critical` 鍵送成一條獨立的 series（`metric="…_critical"`、severity=warning），沒有規則包讀它；租戶同時寫同一個鍵時 `served-values` 以結束碼 2 拒收整棵樹。現在它是 `<key>` 的 critical 列：租戶沒寫就用根的值，租戶寫了蓋過它，`<key>` 沒有值時不送，根層 `<key>: disable` 時跟著關。那條 warning series 消失；有規則或儀表板讀它的要改讀 critical 列。租戶寫同值判 `redundant_override`。`root_defaults_critical_key` 改為只在基底於根目錄與內建層都沒有值時報。新舊兩種拼法相同。
