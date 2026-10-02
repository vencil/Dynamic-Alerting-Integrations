---
section: Fixed
topic: confd-family
issues: [2386]
created: 2026-10-01T22:32:04+00:00
---
- **`_defaults.yaml` 的 `defaults:` 包裝讓 `/effective` 與 `/metrics` 不一致時，da-guard 與 `validate-config` 改為擋下（[#2386](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2386)）**：根目錄檔的 `defaults:` 不是 mapping（沒有這個鍵或沒有值）時，`/effective` 顯示頂層的閾值與保留鍵（如 `_severity_dedup`），exporter 卻不從這個檔送出；任一層的檔 `defaults:` 是 mapping（含 `{}`）時，頂層的這些鍵不進 defaults 合併。兩道閘門先前都回 0。現在 da-guard 分別報 `root_defaults_unwrapped` 與 `defaults_toplevel_ignored`（皆為 error），`validate-config` 的 `root_defaults` 列與新列 `defaults_wrapper` FAIL，並列出檔案與鍵。`defaults:` 沒有值時合併讀整份文件，不列為後者。`_routing` 開頭的鍵、`_metadata`、其他工具從頂層讀的鍵與 exporter 根目錄設定結構的欄位不在判定範圍。
