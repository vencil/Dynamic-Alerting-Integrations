---
section: Fixed
topic: exporter
issues: [4370]
created: 2026-11-28T16:07:06+00:00
---
- **含換行的 selector 值不再讓整段 `_custom_alerts` 讀不回來（exporter；[#4370](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4370)）**：exporter 讀 `_custom_alerts`、`_routing` 這類清單或 mapping 時，會先把它寫回 YAML 字串、之後再讀一次。含換行的字串原本寫成 literal block（`|`）；字串第一個字元是 tab 時（例如 `selectors_re` 的 `"\t.*\n"`），寫出的 block 連 yaml.v3 自己都讀不回來，該租戶的自訂告警因此整段被拒收。現在含換行的字串一律寫成雙引號字串，讀回來與租戶寫的完全相同。沒有含換行字串的值，輸出與先前逐位元組相同。票上的 `"\n.*"`（開頭是換行）在修正前就能原樣讀回，現在有測試釘住。
