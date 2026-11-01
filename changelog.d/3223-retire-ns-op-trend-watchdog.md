---
section: Removed
topic: ci
issues: [3223]
created: 2026-11-01T14:28:32+00:00
---
- **⚠️ BREAKING：退役舊的絕對 ns/op 夜跑趨勢監測（ci；[#3223](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3223)）**：`bench-record.yaml` 的 `trend-watch` job、`analyze_bench_history.py --trend-watch` 與它專用的完整性標記 `bench-baseline.rows`（含 `write_baseline_marker.py`）一併移除，`perf-trend` 不再自動開關票。它開過 9 張票，8 張被自己反覆開關後自動關掉、1 張誤判，沒有一張促成修正。配對 ns/op 維持只寫執行摘要（[ADR-032](docs/adr/032-paired-interleaved-bench-measurement.md) 撤回「並行後切換」）；低於逐 commit 門檻的累積漂移改由確定性指標的累積預算偵測，在它上線前沒有自動偵測（[ADR-039](docs/adr/039-bench-acceptance-ledger.md)）。`analyze_bench_history.py` 保留 GO/NO-GO 變異表模式。
