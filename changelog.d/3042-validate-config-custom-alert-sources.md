---
section: Added
topic: da-tools
issues: [3042]
created: 2026-10-16T17:00:00+00:00
---
- **`validate-config` 新增 `custom_alert_sources`：exporter 送出的自訂告警編不出規則時報出來，只報不擋（[#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042)）**：編譯器照 exporter 自己的讀法產生規則，所以一條送出（或停用、重新啟用後會送出）的自訂告警沒有規則，只剩一種原因：編譯器與 exporter 對它的讀法不同（`recipe_id` 不同，或編譯器拒收 exporter 收下的設定）。那條門檻沒有規則讀，永遠不會告警，編譯規則包也會被拒。每條一行 WARN，寫出檔案、租戶、條目序號與兩邊的讀法，結束碼不變；只在裝有編譯器的地方（repo 內）出現。
