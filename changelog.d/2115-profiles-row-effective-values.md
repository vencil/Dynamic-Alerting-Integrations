---
section: Changed
topic: confd-family
issues: [2115]
created: 2026-10-06T18:00:00+00:00
---
- **`validate-config` 的 profiles 列改讀 exporter 的答案（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）**：⚠️ 行為變更：`_profile` 有沒有綁到改看 `da-guard effective`。任何根目錄平台檔的 `profiles:` 都算（以前只認 `_profiles.yaml`）；根平台檔 `tenants:` 的 `_profile` 也檢查；從子目錄 `_defaults.yaml` 繼承的改報「exporter 不從 defaults 檔綁 profile」。沒有 `tenants:` 的檔不當租戶，列在明細（`--json` 也有）。exporter 丟掉／讀不到的檔、拒收的樹、**完全沒有設定檔的 `--config-dir`（以前 PASS、exit 0，現在 FAIL、exit 1；exporter 對它拒絕啟動）**，這一列 FAIL；找不到 da-guard、或 da-guard 比本工具舊（effective 輸出缺 `skipped`）時結束碼 2，後者建議重建／升級 da-guard——`validate-config` 從此需要 da-guard（映像內建；repo 內 `make da-guard-build`；`make validate-config` 依序用 `DA_GUARD_BINARY`、有 Go 時自己建的、`$PATH` 上的）。profile 本身的結構檢查仍只讀 `_profiles.yaml`。`da-guard effective` 新增 `skipped`。
