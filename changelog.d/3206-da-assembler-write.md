---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-24T05:12:53+00:00
---
- **`da_assembler.py` 的 conf.d 檔改由 `da-guard write` 寫、刪（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §2 步驟 10c）**：CR 渲染出的 `<metadata.name>.yaml` 以帶型別的值交給 Go 寫，整份取代既有檔；CR 刪除時的刪檔也經它。`--render-cr` 被拒是 rc 2、點名該檔；controller 記在 CR status（`Error`）與 log，程序不結束。⚠️ 可見變化：字串一律雙引號、檔頭後空一行；`defaults` 的值必須是數字或 null，字串一律拒（先前寫成無引號而被讀成數字的 `"1e3"`、`0o+7` 也是）；不合 ADR-035 的租戶 id 與不是 DNS-1123 subdomain 的 `metadata.name` 在 controller 路徑也拒寫；整棵樹判一次，宣告樹上已有租戶的 CR 也拒寫（先前寫出重複宣告；被拒的 CR 要等它自己下一個事件、重啟或 `--once` 才重判，週期重判見 [#3318](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3318)）；`defaults` 與租戶值裡超過 18 位的整數以浮點寫出；新檔權限是 0600（先前 0644），取代的檔保留原權限。da-guard write 的暫存檔與刪除備份檔名不再含原檔名，接近 NAME_MAX 的檔名也寫得了。
