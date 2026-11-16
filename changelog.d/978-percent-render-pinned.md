---
section: Added
topic: rule-packs
issues: [978]
created: 2026-11-16T16:21:20+00:00
---
- **百分比渲染必須被 firing 測試釘住（rule-packs；[#978](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/978)）**：新閘門 `check_percent_render_pinned.py`（pre-commit `percent-render-pinned-check`，CI Lint 也跑）。annotation 用 `humanizePercentage` 或 `}}%` 渲染百分比時，必須有一個 `exp_alerts` 非空的測試在 `exp_annotations` 寫出同一個 key 的渲染結果，比值與百分數混用（100% 顯示成「1%」、85 顯示成「8500%」）會直接出現在 fixture 的 diff。現有缺口列在 `tests/rulepacks/percent_render_baseline.yaml`，雙向比對：新缺口與過時條目都會擋。慣例寫在 testing-playbook 的 PromQL rule-test traps。
