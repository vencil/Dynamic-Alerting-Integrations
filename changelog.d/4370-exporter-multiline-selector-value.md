---
section: Fixed
topic: exporter
issues: [4370]
created: 2026-11-28T16:07:06+00:00
---
- **開頭是 tab 的多行字串不再讓整段 `_custom_alerts`／`_routing` 讀不回來（exporter；[#4370](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4370)）**：exporter 讀 `_custom_alerts`、`_routing`、`_metadata` 這類清單或 mapping 時，會先寫回 YAML 字串再讀一次（帶 `default` 鍵的保留 mapping 也一樣）。含換行的字串寫成 literal block（`|`）；字串第一個字元是 tab 時（例如 `selectors_re` 的 `"\t.*\n"`），寫出的 block 連 yaml.v3 自己都讀不回來，`_custom_alerts` 整段被拒收，帶 `default` 的 `_routing`／`_metadata` 則被當成沒寫。現在 literal block 讀不回來的字串改寫成雙引號字串，讀回來與租戶寫的完全相同；其餘字串的輸出與先前逐位元組相同。判斷規則與 tenant-api 寫 `_custom_alerts` 時共用同一份。票上的 `"\n.*"`（開頭是換行）修正前就能原樣讀回，現在有測試釘住。
