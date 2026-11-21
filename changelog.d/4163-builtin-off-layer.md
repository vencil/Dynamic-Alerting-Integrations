---
section: Changed
topic: exporter
issues: [4163, 3924]
created: 2026-11-21T16:40:00+00:00
---
- **⚠️ breaking：平台不給值的閾值鍵也進讀取端內建層，值是 off（exporter、da-guard、tenant-api；[#4163](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4163)）**：threshold registry 標 `arming: operator` 的鍵，客戶根 `_defaults.yaml` 不必再寫進 `defaults:` 或列在 `optional_overrides:`，租戶寫的值就會送出。off 算「根層有值」：租戶只寫 `<鍵>_critical` 也送 critical 列，profile 與子目錄可以給值；base 本身不送列。根層 `defaults:` 對這些鍵寫數字照舊生效，寫 `disable` 成為合法的整族關閉；寫 `null` 等於沒寫，租戶的值照送（不再報 `root_default_null_undeclared`）。這些鍵的 `expires:` 不生效：值過了 `expires:` 照送，tenant-api 寫入時擋下（到期退回 off 等於告警悄悄消失）。`user_threshold_unset` 拿掉 `fix` 標籤，只寫 `_critical` 算已設定，根層 `disable` 結束提醒。`validate-config` 的 `platform_defaults` 列按 rule pack 列出這些鍵與設定了的租戶數。內建層摘要變為 `968ca3d1c003`：不同摘要的元件對同一棵樹的判讀不同，exporter、da-tools、tenant-api 要一起升級。
