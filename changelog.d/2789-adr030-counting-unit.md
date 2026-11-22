---
section: Changed
topic: rule-packs
issues: [2789]
created: 2026-11-21T23:37:33+00:00
---
- **ADR-030 第 7 節對帳單位與計分器一致；`waveform_score.py` 加按事件計的揭露比率（dx；[#2789](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2789)）**：門檻（漏報為 0）明定以（故障, 變體）計，任一必叫變體、含 fan-out 的每條 series 沒接住就不通過，與計分器現行行為相同。按「故障事件 × 告警類別」計的比率改為只揭露：計分報告 `summary` 新增 `episode_class_pairs`、`episode_class_caught`、`episode_class_catch_rate`（`--redact` 一併保留），人類可讀輸出多一行「按事件計」，verdict 與結束碼不變。ADR「已知限制」裡「計分器計數單位與第 7 節不一致」一條移除。
