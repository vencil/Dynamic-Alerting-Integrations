---
section: Fixed
topic: exporter
issues: [2266]
created: 2026-09-28T05:31:10+00:00
---
- **非 UTF-8 的租戶 id 或檔名不再讓 exporter 崩潰（exporter；[#2266](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2266)）**：先前租戶檔若宣告了非 UTF-8 的租戶 id（例如 `!!binary` 寫出的位元組），第一次 scrape `/metrics` 就會讓整個 process panic；解析失敗的檔若檔名不是合法 UTF-8，載入時就會 panic。**行為變更**：這種租戶檔現在與其他解析失敗的租戶檔一樣整份跳過（同檔其他租戶也不載入），寫 WARN 並計入 `da_config_parse_failure_total`（單檔模式則是載入失敗）；exporter、`/effective` 與 da-guard 的判決一致。`_` 平台檔 `tenants:` 下的非 UTF-8 條目只剔除該筆並寫 WARN，檔內其餘內容照常生效。非 UTF-8 檔名的解析失敗照常計數（租戶檔 WARN、`_` 檔 ERROR），label 中的壞位元組以 `U+FFFD` 取代；這類 log 裡的檔名改以帶引號的跳脫形式印出。
