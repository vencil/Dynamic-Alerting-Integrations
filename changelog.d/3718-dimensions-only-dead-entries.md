---
section: Fixed
topic: exporter
issues: [3718]
created: 2026-11-11T16:19:37+00:00
---
- **寫入關卡判死條目改用 resolver 自己的入口判定，與共用判決一致（tenant-api、`da-guard write`；[#3718](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3718)）**：base 只寫在根 `dimensions:` 的維度鍵（例如 `container_cpu{version="v2"}`，`defaults:` 與 `optional_overrides:` 都沒有 `container_cpu`）照樣送出、共用判決也以 `dead_entry` 擋下，寫入關卡卻不判它的死條目；反過來，只在 `optional_overrides:` 宣告 base 時，`<key>_critical` 不會送出，寫入關卡卻仍判它的死條目。現在維度鍵看 `dimensions:`、`_critical` 鍵看 base 是否有平台值（或 `<key>_critical` 本身在 `defaults:` 有值）、其他鍵看 `defaults:`／`optional_overrides:`。⚠️ tenant-api 會新擋下第一種寫法的死條目（CI 的 da-guard 本來就擋）；檔裡原封不動的只回 NOTICE。新增常駐測試，逐一比對各種佈局與鍵型下，寫入關卡與共用判決的死條目是否一致。
