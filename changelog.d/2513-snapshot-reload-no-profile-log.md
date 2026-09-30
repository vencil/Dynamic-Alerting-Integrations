---
section: Fixed
topic: tenant-api
issues: [2513]
created: 2026-09-30T23:45:00+00:00
---
- **tenant-api 重載租戶快照時不再把 unknown profile 的 WARN 寫進 log（tenant-api；[#2513](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2513)）**：租戶列表／搜尋的快照每次重載（30 秒 TTL 到期，或寫入後失效）都會重跑 `config.LoadDir`，原本對引用不存在 profile 的租戶每次都寫一行 `WARN: tenant=… references unknown profile …`。現在 `BuildFlatConfig` 的 profile 展開改寫到呼叫端傳入的 logger：`LoadDir(dir, nil)` 不寫，threshold-exporter 載入目錄時仍寫同一行到它的 log，輸出不變。連帶：`da-guard served-values` 的 profile WARN 改寫到它給 `LoadDir` 的 stderr logger，仍在 stderr，但不再帶時間戳前綴；`da-guard` 主命令（`ScopeEffective`）刻意維持原樣，仍把這行寫到 stderr。
