---
section: Fixed
topic: exporter
issues: [2118]
created: 2026-09-28T17:12:57+00:00
---
- **租戶 id 裸寫成 `010`、`0x1`、`007` 時，`/effective` 與 da-guard 找得到該租戶了（exporter；[#2118](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2118)）**：`tenants:` 底下不加引號的 `010:`，YAML 會解成整數。`/metrics` 依 key 的原文服務租戶 `010`，但 `/effective`、tenant-api 與 da-guard 查租戶時用的是轉換後的 `8`，於是回 `tenant "010" not in file`：da-guard 整個 scope rc 2，exporter 也跳過該租戶的 merged_hash。現在這些讀取端都依 key 原文找租戶，與 `/metrics` 一致；`1.0`、`2024-01-01`、`+1`、`.inf` 這類 key 同樣修正。reload 時「defaults 變更被租戶覆寫」的判定也改用同一個解析，不再把這類租戶的變更誤報為 cosmetic。加引號的 `"010"` 與一般名稱不受影響。⚠️ `tenants:` 區塊內含 merge key（`<<`）或以 alias 當 key 的檔案不在此修正範圍：這類檔案仍用原本的解碼，裸寫的 key 在這些讀取端照舊查不到。
