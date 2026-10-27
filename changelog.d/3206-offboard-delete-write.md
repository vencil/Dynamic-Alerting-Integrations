---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-27T15:09:29+00:00
---
- **`offboard_tenant` 的刪檔改由 `da-guard write` 做（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §2 步驟 10）**：工具不再自己刪檔，而是把要刪的檔一批送給 Go：每個檔當成搬移的來源判（一般檔、有讀取端、parse-tree 讀得了），整棵樹以拿掉它們之後判一次。被拒時每個理由具名印在 stderr、結束碼 2，什麼都不刪；只做 Pre-check 時也以同一批的 dry-run 判，被拒同樣是 2。⚠️ 可見變化：同一租戶由兩個租戶檔宣告（例如 `<tenant>.yaml` 加 `<tenant>.yml`）時兩個都刪，先前只刪一個、租戶仍被服務；其中任一個另宣告別的租戶則一個都不刪。設定檔是 symlink 時拒刪（先前刪掉的是 link 本身）。判定時檔已不在、沒刪任何檔時 `--execute` 結束碼 2（先前是 0）。
