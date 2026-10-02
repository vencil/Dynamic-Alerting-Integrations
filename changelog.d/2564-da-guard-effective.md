---
section: Added
topic: exporter
issues: [2564, 2588]
created: 2026-09-30T23:41:49+00:00
---
- **`da-guard effective`：以 JSON 印出每個租戶在 tenant-api `/effective` 的答案（[#2564](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2564)）**：與 `/effective` 走同一個 `pkg/config` resolver，不另做判斷。每個租戶除了 `/effective` 的欄位（`effective_config`、`merged_hash`、`defaults_chain`、`platform_overlay`、`profile_overlay` 等），另輸出綁定的 `profile` 與 `key_sources`（每個頂層 key 來自 defaults／平台檔 `tenants:`／profile／租戶檔哪一層、哪個檔）。文件帶 `schema: da-guard.effective/v1`；exit code 比照 `served-values`（2 拒收整棵樹，3 有檔無法 decode 或讀不到，分列在 `parse_failed`／`unreadable`；後者與 `served-values` 同一份 `{file, reason}`，[#2588](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2588)），`parse_failed` 與 `served-values` 同一個判定：exporter 整份丟掉的根 `_defaults.yaml` 即使 `/effective` 仍讀得到，也在這裡列出並 exit 3。`scripts/tools/_lib_tenant_values.py` 新增 `load_effective()`，只解析這份 JSON；讀取端的切換在後續 PR。`da-tools guard effective` 轉發到同一個子命令。tenant-api `/effective` 的回應不變。
