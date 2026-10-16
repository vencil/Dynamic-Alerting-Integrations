---
section: Fixed
topic: custom-alerts
issues: [3042]
created: 2026-10-16T17:46:14+00:00
---
- **自訂告警的文件改寫成 exporter 實際送出的來源（custom-alerts；[#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042)）**：ADR-024／ADR-031、config-driven、recipe 文件、schema 說明與範例原本寫「`_defaults.yaml` 宣告的 recipe 套用整棵子樹、租戶不可覆寫」，但 exporter 從不送出 `_defaults.yaml` 裡的 `_custom_alerts`：每個租戶只取一處（租戶自己的條目、根目錄平台檔的 `tenants:`、綁定的 profile，依序取第一處、不合併）。一組租戶共用的告警寫在 profile，但編譯器改版前 不為 profile 裡的 recipe 產生規則、不能指望它們告警，文件已標明並給出目前可用的寫法。另外寫明：放在根 `_defaults.yaml` 的 `defaults:` 底下會讓整份檔解碼失敗，檔裡其他預設值也不送出。
