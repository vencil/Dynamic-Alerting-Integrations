---
section: Changed
topic: exporter
issues: [2115]
created: 2026-10-06T10:30:00+00:00
---
- **`/effective`、`/simulate` 與 `da-guard effective` 的 `effective_config` 改與 `/metrics` 一致（tenant-api / da-guard；[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）**：值為純量、list 或排程（`{default, overrides}`）的閾值，在不同層以新舊兩種拼法寫時只留 `/metrics` 上勝出那一層的值與拼法（過去兩個 key 並列），同一層兩種拼法並存時新拼法勝出，`key_sources` 指向那一層；排程整份取代下層的值，不再與子目錄排程的時段合併。其他 mapping（例如只有 `overrides`、沒有 `default`）照舊逐鍵合併、保留各層拼法。`merged_hash` 不變，仍是逐鍵合併結果的雜湊，與 exporter 的 reload 判斷、`describe_tenant` 相同；所以這些形狀的 `effective_config` 不再是 `merged_hash` 所雜湊的那份內容。da-guard 主報告（必填欄位、cardinality 等）的結論不變。`describe_tenant` 尚未跟進。
