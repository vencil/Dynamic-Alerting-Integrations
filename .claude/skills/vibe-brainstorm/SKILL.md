---
name: vibe-brainstorm
description: 設計階段的 Socratic ideation — 用提問逼出 MVP 範圍、explicit trade-off、defer-with-trigger，加 proposer≠critic 內部對抗 + validate-direction，再走外部 adversarial review。Use when designing a new ADR / new component / epic decomposition / `RFC:` 討論 / 評估技術選型。SKIP for code-level debugging（用 `engineering:debug`）或 PR review（用 `vibe-subagent-review`）——這是「還沒寫 code、在決定要做什麼」的階段。
---
<!-- 此檔為產生物，來源 .agents/skills/vibe-brainstorm/SKILL.md —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->

# vibe-brainstorm

先發散提問、再收斂 locked decision、最後外審；不是一次給答案。錨例來自 [ADR-020](../../../docs/adr/020-tenant-federation.md)（四輪設計討論、兩輪外審）。

## 五個提問（發散階段逐項問）

1. **Reuse-over-build**：有沒有現成的開源／proven 方案？（ADR-020 用 prom-label-proxy 不自寫 endpoint；對齊 lint-adoption 的 adopt-then-wrap）
2. **MVP 範圍 vs Future Work**：最小可行版是什麼？哪些明確 drop 到 Future Work？（ADR-020：2-tier policy 上、3-tier permission model 下放）
3. **每個決策的 explicit trade-off**：換到什麼、犧牲什麼，寫出來。（ADR-020：「TTL 4h + 無 server-side revocation list，明寫換實作簡單」）
4. **Defer-with-trigger，不是 defer-vaguely**：延後的項目給具體觸發條件。（ADR-020：「3-tier permission → 等 compliance 客戶觸發」；#442 `wontfix-without-signal`）
5. **Blast-radius / failure mode**：新能力炸掉時影響多大、有沒有護欄？（ADR-020 三件組：concurrency cap / request timeout / series-count cap）

## 收斂：locked decision 清單

每條一句加 trade-off；未定的標 open question。必列**考慮過但 reject 的替代方案與理由**（防 first-idea anchoring，讓後人能重評）。深挖 HOW 之前先用 field-data 確認 WHAT 對（方向沒驗過，HOW 再漂亮都可能白做；別把 core-correctness 當 gold-plating 延後）；locked 之後換帽子當 critic 試著打穿自己的設計（最脆的假設、不可接受的 trade-off、護欄真擋得住嗎），打不穿才算收斂。

## 外審

locked decision 後、實作前走一輪外部 adversarial review（Gemini / o3）。外審意見走 take / reframe / reject，外審也會杜撰路徑與前提，先驗再收。設計空間真的很寬、選錯代價高時，才升級成多 agent 提案 panel（reuse [`vibe-security-audit`](../vibe-security-audit/SKILL.md) 的 Workflow harness：多個 agent 各從不同角度（MVP-first / risk-first / cost-first / user-first）平行獨立提案、彼此盲、不 anchor 同一起點 → judge 打分收斂、把 runner-up 的好點子 graft 進來；每個候選另由不同模型 critic 打穿才進 locked decision）；它不取代外審——內部 panel 抓內部矛盾，外審抓內部共有的盲點；小題不要起它。已 locked 的執行直接做，不再回到本 skill。
