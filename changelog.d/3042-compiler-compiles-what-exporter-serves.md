---
section: Changed
topic: custom-alerts
issues: [3042]
created: 2026-10-17T13:00:00+00:00
---
- **自訂告警編譯器改照 exporter 實際送出的編譯（BREAKING；custom-alerts；[#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042)）**：編譯器不再自己讀 conf.d，改讀 `da-guard served-values` 新增的每租戶 `custom_alerts`（每條的設定、exporter 的判定、recipe_id 與 severity），只為 exporter 送出與停用的條目產生規則。⚠️ 行為變更：profile 與根 `_defaults.yaml` 的 `tenants:` 裡的 recipe 開始產生規則、會告警；`_defaults.yaml` 頂層的 `_custom_alerts` 不再編譯（exporter 從不送出，原本的規則永遠不會觸發）；租戶自己的清單整份取代平台檔的那份。編譯器不再判上限、同名與同形狀，也不再隔離（這些由 exporter、da-guard 與 tenant-api 判）；`--max-custom-recipes` 移除。`_defaults.yaml` 裡的 `_custom_alerts` 沒有讀取端：根目錄那份由 da-guard 與 `validate-config` 的 `root_defaults_unwrapped`／`defaults_toplevel_ignored` 報 error（結束碼 1），子目錄那份由 da-guard 的 `subtree_default_reserved_key` 報（`--warn-as-error` 時結束碼 1），訊息寫出該改放的地方。編譯需要 da-guard（`make custom-alerts-compile` 會自己建）。
