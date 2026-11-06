---
section: Added
topic: dx
issues: [1486]
created: 2026-11-06T00:07:15+00:00
---
- **送給外部模型之前的把關（去識別化政策；[#1486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1486)）**：`docs/internal/engagement-deid-policy.md` 新增出站一節，把既有三層分類套用到「送出」方向：provider 須經 owner 核准（清單為空時每次送出都要 owner 當下同意）、B 層與 secrets 先移除，做不到就改用內部 reviewer。`vibe-brainstorm` 的外審步驟改為先過這一節，並由 `tests/dx/test_outbound_review_gate.py` 釘住兩者之間的連結。
