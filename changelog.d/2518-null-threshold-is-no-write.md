---
section: Fixed
topic: confd-family
issues: [2518]
created: 2026-10-03T10:04:56+00:00
---
- **閾值寫成 null 在每一層都等於沒寫，`/metrics` 與 `/effective`、da-guard 一致（[#2518](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2518)）**：租戶檔、根平台檔 `tenants:` 或 profile 把閾值寫成 null（任一拼法）時，`/metrics` 先前跳過 profile 與子目錄 `_defaults.yaml`、直接退回根目錄預設值，da-guard 因此可能建議刪掉一個刪了會改變 `/metrics` 的覆寫；現在改用下一層的值。⚠️ 根 `_defaults.yaml` 的 `defaults:` 寫 null 的閾值不再送出門檻 0（一直觸發），改為根層沒有宣告：該閾值不送任何 series，自己設了值的租戶也一樣——要只宣告、不給平台值，請改列在 `optional_overrides:`。`validate-config` 的 `root_defaults` 與 `deprecate` 的提示改為說明這一點；tenant-api 的 GET 也照同一規則。保留鍵（`_` 開頭）寫 null 仍是刪掉繼承值，不變。
