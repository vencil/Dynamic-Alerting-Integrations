---
section: Changed
topic: confd-family
issues: [2526, 2547]
created: 2026-10-08T13:35:03+00:00
---
- **`diagnose` 的 profile 與繼承鏈改讀 exporter 的答案（[#2526](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2526)、[#2547](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2547)）**：⚠️ 行為變更：給了 `--config-dir` 時，租戶有沒有、綁到的 profile、來源層取 `da-guard effective`，值取 exporter 實際送出的（`da-guard served-values`），不再由 Python 重讀 YAML。`2024-02-30` 不再 traceback（顯示退回的 defaults 值、歸 defaults 層）、`!foo 70` 不再丟掉整份檔、`tenants: !!set {tx}` 認得 tx、`~:` 鍵不再顯示（exporter 不送）。整棵樹都讀（含子目錄）。`resolved` 的值是數字（`/metrics` 不送的 key 顯示原文）；`chain` 每層只列勝出的 key，平台檔 `tenants:` 自成 `platform` 層；`profile_name` 為實際綁上的 profile（引用不存在的 profile 為 `null`）；沒有 `tenants:` 的檔不當租戶；不在樹裡的租戶回 `{"error": ...}`。`skipped_unusable_files` 移除：找不到 da-guard、exporter 丟掉或讀不到某個檔、served-values 拒收整棵樹時改為 exit 2，不給部分答案。`declared` 仍只取根目錄 `optional_overrides:` 的名稱原文。
