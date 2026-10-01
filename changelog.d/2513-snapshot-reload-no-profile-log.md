---
section: Fixed
topic: tenant-api
issues: [2513]
created: 2026-09-30T23:45:00+00:00
---
- **tenant-api 載入租戶快照時不再把 profile 展開的 WARN 寫進 log（tenant-api；[#2513](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2513)）**：租戶列表／搜尋的快照每次載入（冷載入、30 秒 TTL 到期、寫入後失效）都會重跑 `config.LoadDir`，原本每次都把 profile 展開的兩類 WARN 寫進 log：租戶引用不存在的 profile（`WARN: tenant=… references unknown profile …`），以及 profile 補值到「宣告但無平台值」的 key（`WARN: profile "…" supplies "…", but that key is declared without a platform value …`）。現在 `BuildFlatConfig` 把這兩類 WARN 寫到呼叫端傳入的 logger：`LoadDir(dir, nil)` 不寫，threshold-exporter 載入目錄時仍寫到它的 log，輸出不變。連帶：`da-guard served-values` 的這兩類 WARN 改寫到它給 `LoadDir` 的 stderr logger，仍在 stderr，但不再帶時間戳前綴；`da-guard` 主命令（`ScopeEffective`）刻意維持原樣，仍把它們寫到 stderr。
