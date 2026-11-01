---
section: Changed
topic: exporter
issues: [3475]
created: 2026-11-01T01:30:00+00:00
---
- **⚠️ breaking：維度鍵改由根 `_defaults.yaml` 的 `dimensions:` 宣告哪些 label 會送出（ADR-038 決定 11）**：`<base>{label="v"}` 只有在它的每個 label 都在 `dimensions:` 為該 base 宣告過時，exporter 才送出那一列。租戶檔、profile、子目錄與根 `defaults:` 寫的維度鍵都一樣。base 已有平台預設值或列在 `optional_overrides:`，不再足以讓維度鍵送出；沒宣告的維度鍵不送出，共用判決報 `unread … (no_row)`，tenant-api 寫入擋下。原因是出貨的 rule pack 以 `max by(tenant)` 合成門檻，只保留它分組的 label，其餘 label 的值會併成一個租戶門檻。出貨的 `dimensions:`（helm values 的 `thresholdConfig.dimensions`、開發用與 try-local 的 `_defaults.yaml`）由門檻登錄表每個 key 的 `dimensions` 生成，目前只有 `container_cpu`、`container_memory` 的 `version`；測試雙向對照 rule pack 的分組。exporter 原本手寫的 version 試行清單（非試行指標上的 `version` 只警告）一併移除，改由同一份宣告決定。**遷移**：自訂規則若依某個 label 讀維度鍵，在根 `_defaults.yaml` 加上 `dimensions: {<base>: [<label>, …]}`（Helm：`thresholdConfig.dimensions`）；沒有規則讀的維度鍵，拿掉即可，它原本就不會依維度生效。
