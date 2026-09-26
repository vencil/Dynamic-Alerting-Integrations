---
section: Changed
topic: agent-guidance
issues: [962, 1411, 1443, 1737]
created: 2026-09-26T17:00:00+00:00
---
- **Agent 指引改為跨 AI 中性的單一真相源，並收進 repo（internal、dx；[#1737](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1737)）**：skills 與 subagent 角色的 SSOT 移到 `.agents/`，由 `gen_agent_adapters.py` 投影成 `.claude/` 與 `AGENTS.md` 的 skill 索引等各家 agent 會讀的位置並由 drift gate 守住；原本只存在單機記憶的認識論紀律收進 `agent-rulebook.md`。新增 `verifying-claims`、`vibe-converge`（多輪修正收斂協議）、`vibe-security-audit` 等 skill，刪除與不可協商項重複的 `vibe-dev-rules`；宣稱須附證據區塊、PR 範本改為四段式、CHANGELOG 新增條目設 1,000 字元上限，並寫明繁中規範不涵蓋工具印給 operator 的字串。
