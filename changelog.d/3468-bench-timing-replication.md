---
section: Changed
topic: ci
issues: [3468]
created: 2026-10-31T08:55:28+00:00
---
- **bench gate 分辨確定性指標與 sec/op，只有 sec/op 被標時先重現一次再判定（`bench_gate_compare.sh`；[#3468](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3468)）**：benchstat 的判定（退化列、control canary、CPU、輸出形狀檢查）集中到 `scripts/tools/dx/bench_verdict.py`，並依單位區段判讀被標的列。B/op 或 allocs/op 退化照舊立即成立；只有 sec/op 被標時，再跑一輪獨立的交錯量測，兩輪都標到的 bench 才算退化，沒重現的列只在 summary 列出、不開票也不貼 label。第二輪 canary 不穩時判 INCONCLUSIVE，不會讀成乾淨。三支 bench workflow 的 timeout 由 20 改為 35 分鐘。`REPLICATE_TIMING_ONLY=0` 可停用第二輪。
