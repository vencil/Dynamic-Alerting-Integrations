---
section: Changed
topic: confd-family
issues: [2115]
created: 2026-10-01T00:17:10+00:00
---
- **`blind-spot` 與 `analyze-gaps --config-dir` 改讀 exporter 實際發出的閾值（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）**：兩者改經 `da-guard served-values` 取值，不再只讀根目錄租戶檔的字面內容。值寫在 `defaults:`、平台檔 `tenants:`、租戶檔或子目錄都看得到，子目錄裡的租戶也不再消失。⚠️ 行為變更：根 `defaults:` 有某類 DB 的閾值時，每個租戶都算該類已監控（預設值實際會發給每個租戶）；`disable` 的鍵、沒有預設值而 `/metrics` 不發的鍵，不再算已監控，也不列入缺口分析；`analyze-gaps --json` 的 `current_value` 改為 served-values 的 float（`7` 變成 `7.0`，文字報告不印這個值）；沒有 `tenants:` 的檔不再當成以檔名為 id 的租戶，改在 stderr 逐檔印 `WARN`，da-guard 在 stderr 印的內容也逐行照轉；exporter 解析失敗而整份跳過的檔、被拒收的樹（例如跨檔重複宣告）、找不到 da-guard 時以 exit 2 結束。`analyze-gaps --tenant-config` 不變。
