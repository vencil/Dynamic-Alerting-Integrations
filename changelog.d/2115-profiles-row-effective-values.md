---
section: Changed
topic: confd-family
issues: [2115, 2725]
created: 2026-10-06T18:00:00+00:00
---
- **`validate-config` 的 profiles 列改讀 exporter 的答案（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）**：⚠️ 行為變更：`_profile` 有沒有綁到改看 `da-guard effective`。任何根目錄平台檔的 `profiles:` 都算（以前只認 `_profiles.yaml`）；根平台檔 `tenants:` 的 `_profile` 也檢查；從子目錄 `_defaults.yaml` 繼承的改報「exporter 不從 defaults 檔綁 profile」。沒有 `tenants:` 的檔不當租戶，列在明細（`--json` 也有）。exporter 丟掉／讀不到的檔、拒收的樹，這一列 FAIL；**完全沒有設定檔的 `--config-dir`（以前 PASS、exit 0；exporter 對它拒絕啟動）**、找不到 da-guard、da-guard 跑不起來、不認得本工具用的子命令或旗標（例如 v2.9.x）或輸出缺欄位時 FAIL、結束碼 2，後幾種建議換新的／重建 da-guard，不叫人改設定檔——`validate-config` 從此需要 da-guard（映像內建；repo 內 `make da-guard-build`；`make validate-config` 在 `DA_GUARD_BINARY` 未設時先建 `.build/da-guard` 再用，沒有 Go 則 ERROR）。profile 本身的結構檢查仍只讀 `_profiles.yaml`。`da-guard effective` 新增 `skipped`。
