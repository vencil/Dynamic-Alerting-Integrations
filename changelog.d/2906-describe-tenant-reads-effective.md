---
section: Changed
topic: confd-family
issues: [2906, 2486]
created: 2026-10-17T13:00:00+00:00
---
- **⚠️ Breaking：`describe_tenant.py` 的每個值改讀 `da-guard effective`，`--what-if` 須指名 `--replaces` 或 `--at`（confd-family；[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：有效設定、繼承鏈、overlay 與兩個 hash 都是 Go 的答案、照 Go 的寫法輸出，不再用 PyYAML 讀 conf.d，所以沒有 da-guard 時結束碼 2；`_custom_alerts` 的聯集經 `da-guard parse-tree` 讀（`compile_custom_alerts` 也是，CI 與 pre-commit 經 `with_da_guard.sh`）。exporter 拒收整棵樹（某租戶重複宣告）時其他租戶也不描述，結束碼 1；exporter 只略過某檔時照它的解析描述，不再整次停下。`--what-if` 新增 `--at <路徑>`（新增檔），舊的隱含 insert／append-external 用法改為結束碼 2；`da-guard effective` 新增 `--overlay <rel>=<file>`。
