---
section: Added
topic: benchmark
issues: [1439]
created: 2026-09-26T17:00:00+00:00
---
- **夜跑成對交錯量測與 bench 觸發改造（ADR-032；internal）**：`bench-record.yaml` 每夜在同一 runner 內交錯量測 main 與釘在 `.github/bench-reference.yaml` 的參考版本，產出比值 `bench-paired.json`，讓機器速度在比值中相消；判斷引擎 `paired_trend_watch.py` 先只寫摘要、與既有 watchdog 並行。PR bench gate 瘦身，並新增 merge 後逐 commit 歸因的 `bench-attrib-main.yaml`（真退化自動開 `perf-regression` issue）與 `/bench` 留言觸發的 on-demand 跑法。見 [#1439](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1439)。
