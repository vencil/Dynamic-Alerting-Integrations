---
section: Changed
topic: threshold-governance
issues: [2964]
created: 2026-10-14T13:02:25+00:00
---
- **`threshold-recommend`／`threshold-govern`：上界 key 的建議不再直接取 P95（[#2964](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2964)）**：先前閒置或全為負值的指標會被建議收緊到 0，P95 = 0.004 這類值因捨入成 0 也一樣；收緊到 P95 會讓約 5% 原本沒響的點開始響，放寬到 P95 仍約有 5% 的點超標。現在以未捨入的 P95 判斷：P95 ≤ 0 不給建議（reason 附窗內最大值）；收緊時不低於現值的一半，也不低於窗內不超過現值的最大點，不會新增響點，大幅收緊會分幾輪；放寬時改取 P99 並向上取整（`0.001`、`1e-3` 這類小數現值不會被捨入到 0）。`--export-patch` 與 govern 寫出的值與建議值逐位相同（不再以 6 位有效數字捨入）。`--min-delta-pct` 設在 50 以上時，被夾在 -50% 的建議不會開 PR。
