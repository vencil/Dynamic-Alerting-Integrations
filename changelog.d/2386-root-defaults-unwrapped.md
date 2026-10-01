---
section: Fixed
topic: confd-family
issues: [2386]
created: 2026-10-01T22:32:04+00:00
---
- **根目錄 `_defaults.yaml` 少了 `defaults:` 包裝時，da-guard 與 `validate-config` 改為擋下（[#2386](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2386)）**：exporter 只從 `defaults:` 底下讀根目錄的平台閾值，寫在頂層的值 `/metrics` 不送、`/effective` 卻照樣顯示，而兩道閘門先前都回 0。現在 da-guard 報新 finding `root_defaults_unwrapped`（error），`validate-config` 的 `root_defaults` 列 FAIL，兩者都列出被略過的鍵；以 `_` 開頭的鍵與 `state_filters` 等 exporter 會讀的欄位不在判定範圍。`platform-defaults.schema.json` 改為每個 `_defaults*` 檔都必須有 `defaults:` 鍵，recipes 範例的 `finance/_defaults.yaml` 已補上。⚠️ 子目錄檔補包裝時，請把要往下繼承的頂層鍵一起移進 `defaults:`，否則它們不再生效。
