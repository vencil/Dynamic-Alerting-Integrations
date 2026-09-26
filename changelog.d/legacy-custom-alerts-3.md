---
section: Security
topic: custom-alerts
issues: [741, 1008, 1017, 1549, 1582]
created: 2026-09-26T17:00:00+00:00
---
- **自訂告警寫入與編譯鏈硬化（custom alerts）**：修補只需同租戶寫入權限即可觸發的跨租戶缺口：selector 值拒收 Go template 元字元、`window`／`quantile` 改白名單驗證（Go preflight 與 Python compiler 對稱），編譯產物另掃 template 不變式，杜絕跨租戶資料外洩與讓共享 pack 拒載的 DoS；有 selector 的 `recipe_id` 附加 shape 雜湊以消除碰撞（[#1008](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1008)）。⚠️ compiler 遇到無效 recipe 由擋下全平台改為逐條 quarantine（不部署、大聲列出）；⚠️ `quantile` 必須寫成字串（[#1017](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1017)）；⚠️ `compile_custom_alerts` 寫入模式必須明傳 `--out`，且編出零條規則時不再覆寫仍有規則的 pack，須明示 `--allow-empty`（[#1549](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1549)）。另修正 Python 驗證器把 `_custom_alerts` 誤報為 typo、deny-list lint 誤擋 forecast recipe、`--check` 預設掃錯樹。
