---
section: Fixed
topic: confd-family
issues: [2518]
created: 2026-10-03T10:04:56+00:00
---
- **閾值寫成 null 在每一層都等於沒寫，`/metrics` 與 `/effective`、da-guard 一致（[#2518](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2518)）**：租戶檔、根平台檔 `tenants:` 或 profile 把閾值寫成 null（任一拼法）時，`/metrics` 先前跳過 profile 與子目錄 `_defaults.yaml`、直接退回根目錄預設值，da-guard 因此可能建議刪掉一個刪了會改變 `/metrics` 的覆寫；現在改用下一層的值。⚠️ 根 `_defaults.yaml` 的 `defaults:` 寫 null 的閾值不再送出門檻 0（一直觸發），改為根層沒有宣告：該閾值不送任何 series。⚠️ 升級後行為改變：根層寫 null、租戶自己設了值的閾值（含該閾值的 `_critical`），先前照送租戶的值，現在不送；da-guard 新增 `root_default_null_undeclared` 警告逐一指名（子目錄給的值仍由 `subtree_default_undeliverable` 指名）。修法：在根層給數字，或改列在 `optional_overrides:`（`_critical` 只能在根層給數字）。`validate-config` 的 `root_defaults` 與 `deprecate` 的提示改為說明這一點；tenant-api 的 GET 也照同一規則；tenant-api 寫入時的 key 驗證，對根層寫成 null 的 key 也視為沒宣告，拒收租戶為它設的值（同一個原因，修法同樣是在根層給數字）。保留鍵（`_` 開頭）寫 null 仍是刪掉繼承值，不變。
