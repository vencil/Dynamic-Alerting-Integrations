---
name: vibe-security-audit
description: 全 component 週期性深度安全稽核 — Recon→平行 Hunt(Vibe 專屬攻擊面向)→對抗式 Validate(finder≠verifier 換模型)→Synthesize,跑在隔離 worktree 快照上。Use when 新信任邊界 GA 前(federation / L7 identity / machine identity)、security incident 後的「還漏什麼」sweep、或季度深稽核。SKIP for per-PR diff review(用內建 `/security-review`,那是 diff-scoped 單 agent)、code-level debug(用 `engineering:debug`)、單檔變更 review(用 `vibe-subagent-review`)。
---

# vibe-security-audit — 週期性深度安全稽核

多 agent + 對抗驗證的安全稽核 harness,pattern 借自 Cloudflare `security-audit-skill`(MIT),wrap 上 Vibe 自己的信任模型與攻擊面向(adopt-then-wrap)。**確定性編排走 Workflow 工具**(harness > model),角色特化走 `.claude/agents/vibe-sec-{recon,hunter,validator}.md`。

## 定位(與 `/security-review` 互補,不重疊)

- **內建 `/security-review`** = per-PR、**diff-scoped**、單 agent、貼 PR comment → 開發者 pre-merge 快檢。
- **本 skill** = **全 component**、多 agent、對抗式、**週期性深稽核** → 抓 static SAST + diff review 看不到的 business-logic / chained / 跨層信任邊界缺口。兩者是不同 niche。

## 何時觸發 / 跳過

- **觸發**:新信任邊界 GA 前(ADR-027 L7 identity、federation、machine identity);security incident 後的 sweep;季度深稽核。
- **跳過**:per-PR(→ `/security-review`)、單檔 debug(→ `engineering:debug`)、doc/test-only 變更。
- ⚠️ **不進 CI**、不 per-PR:單次成本高(見下),是刻意觸發的 backlog sweep(defer-with-trigger)。

## 執行流程(先 recall,再動工)

1. **起手**:recall `memory/`(security 相關 project 檔)+ 確認 target component 與範圍。
2. **建隔離快照 worktree**(避免同時段並行工作污染 read;host Bash,勿在 dev container 內做 worktree remove):
   ```bash
   git worktree add --detach .claude/worktrees/sec-audit origin/main
   ```
3. **跑 Workflow**(⚠️ `args` 必須是 **JSON 物件**,不是字串——否則 `target` 會 undefined、agents 會退回讀主樹):
   ```
   Workflow({
     scriptPath: ".claude/skills/vibe-security-audit/audit-workflow.js",
     args: { target: "<abs>/.claude/worktrees/sec-audit/components/tenant-api",
             componentLabel: "tenant-api" }
   })
   ```
   要換 target 或攻擊面向,傳 `attackClasses: [{key,title,scope,files}]` 覆蓋預設 4 類。
4. **收尾親驗(鐵律①)**:對每個 `CONFIRMED` finding,**自己** grep+cite 最吃重的 `file:line`,不照收 agent 自評;把跨組件 sink(如 `recipes.py` 內插)也驗。
5. **產出**:呈現 `domain_awareness_verdict` + confirmed vs rejected + coverage(空的攻擊類=該區 hardened,非未稽核);真 finding 用 `spawn_task` 開 track+fix task(帶對抗式 role prompt)。fix 後的對抗式重驗 verifier 遵循 [vibe-subagent-review](../vibe-subagent-review/SKILL.md) 的〈長時驗證 agent 可觀測性協議〉— Workflow-first;raw 背景 `Agent` 必寫 `dev/<scope>/PROGRESS.jsonl` 里程碑 ledger;單 agent ~15 分鐘上限,超過拆 staged agents。
6. **清理**:`git worktree remove .claude/worktrees/sec-audit`(host,不在 container 內)。

## 成本 & 模型分層

- **實測錨點**:tenant-api-sized component(~78 源檔、4 攻擊類)≈ **1.3M token / ~20 分 / 8 agent**;按 target 大小外推。
- **模型分層**(算力集中在 Hunt):Hunt = **opus**;Recon / Validate / Synthesize = **sonnet**(Validate 換模型即滿足 finder≠verifier)。分層在 `audit-workflow.js` 的 `agent()` `model` 寫死,per-role 預設也在 subagent frontmatter。

## 執行驗證清單(v1 已驗 2026-07-04 identity-boundary run 三項全綠;下列留作未來改動後 re-validation guide)

- [ ] `agentType: 'vibe-sec-*'` 真的解析到 `.claude/agents/` 定義(看 agent 是否帶上 read-only tool 限制與角色 stance);若沒解析,workflow 的 inline task prompt 仍可運作但少了角色 stance。
- [ ] progress log 的 promptPreview **不含 `undefined`**(確認 `args.target` 有進去、隔離 worktree 真被讀)。
- [ ] 空 Hunt 結果有被 Synthesize 當「hardened」credit(看 `coverage_note`)。

## 反模式(別做)

- 把稽核跑在主樹而非隔離 worktree(同時段並行編輯會污染 read)。
- 把「沒 finding」當「安全」證明——single run 只找約一半,是 additive;深稽核要 ≥2 run 並讀前次結果。
- 用一堆 LOW 灌報告厚度;或把 designed behavior(如 ADR-022 contained dev-bypass)當 bug 報。
