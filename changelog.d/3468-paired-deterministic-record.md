---
section: Added
topic: ci
issues: [3468]
created: 2026-10-31T16:12:14+00:00
---
- **夜跑成對量測開始記錄 B/op 與 allocs/op（`pair_bench_ratio.py`，`bench-paired/v3`；[#3468](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3468)）**：輸出新增 `deterministic` 欄位，逐支記下參考版本與 main 兩側 `B/op`、`allocs/op` 的中位數與比值（基準為 0 時比值為 null，main 值照記），並印在當夜 job log。只記錄、不判定：開票與關票的規則都不變，`paired_trend_watch.py` 接受 v3 但不讀這個欄位。這是 [ADR-039](docs/adr/039-bench-acceptance-ledger.md) 累積預算的前置量測，四週後依實際夜間噪音決定預算。schema 由 v2 升為 v3，兩個 producer 一起升。
