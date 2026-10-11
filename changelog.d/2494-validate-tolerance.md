---
section: Fixed
topic: da-tools
issues: [2494]
created: 2026-10-11T08:55:30+00:00
---
- **`da-tools validate --tolerance` 真的會影響比對結果（[#2494](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2494)）**：這個旗標原本會被解析，但比對時沒有傳下去，所以不論設多少，都照內建的 `0.001` 判定 match／mismatch。現在每一組比對都會使用 `--tolerance` 的值；沒給時預設仍是 `0.001`。值必須是 ≥ 0 的有限數：`inf` 會讓每一組都判成 match，負值或 `nan` 則連相同的值也判成 mismatch，所以這些值改回 rc 2。
