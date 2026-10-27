---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-27T15:09:29+00:00
---
- **`offboard_tenant --execute` 的刪檔改由 `da-guard write` 做（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §2 步驟 10）**：工具不再自己刪租戶的設定檔，而是送一個刪除項目給 Go；原有的 Pre-check 照舊。da-guard 把要刪的檔當成搬移的來源判（一般檔、有讀取端、parse-tree 讀得了），並以拿掉它之後的整棵樹判一次。被拒時每個理由具名印在 stderr、結束碼 2，什麼都不刪。⚠️ 可見變化：設定檔是 symlink 時改為拒刪（先前刪掉的是 link 本身，指向的檔留著）；da-guard 判定時檔已不在、沒有刪任何檔時，`--execute` 結束碼 2（先前是 0）。
