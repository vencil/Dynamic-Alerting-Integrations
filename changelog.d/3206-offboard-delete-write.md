---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-27T15:09:29+00:00
---
- **`offboard_tenant` 的刪檔改由 `da-guard write` 做（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §2 步驟 10）**：工具不再自己刪檔，而是把要刪的檔一批送給 Go：每個檔當成搬移的來源判（一般檔、有讀取端、parse-tree 讀得了），整棵樹以拿掉它們之後判一次。每個刪除項目帶 `tenants: [<租戶>]` 前提（da-guard write 的新欄位）：檔在 Pre-check 之後被加上別的租戶時整批拒絕（`declares_mismatch`）。被拒時每個理由具名印在 stderr、結束碼 2，什麼都不刪；Pre-check 的判定包含同一批的 dry-run，被拒同樣是 2，不會先印「通過」。⚠️ 可見變化：宣告這個租戶的租戶檔一律刪除，不論在哪一層（先前只看頂層，`team-eu/<tenant>.yaml` 這種仍被服務的租戶報「找不到」）、有幾個（例如 `<tenant>.yaml` 加 `<tenant>.yml`；先前只刪一個、租戶仍被服務）；檔名帶著租戶 id 卻沒宣告它的檔不刪；其中任一個另宣告別的租戶則一個都不刪。設定檔是 symlink 時拒刪（先前刪掉的是 link 本身）。判定時檔已不在、沒刪任何檔時 `--execute` 結束碼 2（先前是 0）。不合 ADR-035 的租戶 id 在讀任何東西之前就以結束碼 2 拒絕；檔名帶租戶 id 卻沒宣告它的檔一律被點名、不刪（沒有其他檔宣告這個租戶時是 ❌、結束碼 1，不再報「找不到」；有時是 ⚠️）。
