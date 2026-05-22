---
name: vibe-brainstorm
description: 設計階段的 Socratic ideation — 用提問逼出 MVP 範圍、explicit trade-off、defer-with-trigger，再走外部 adversarial review。Use when designing a new ADR / new component / epic decomposition / `RFC:` 討論 / 評估技術選型。SKIP for code-level debugging（用 `engineering:debug`）或 PR review（用 `vibe-subagent-review`）——這是「還沒寫 code、在決定要做什麼」的階段。
---

# vibe-brainstorm — 設計階段 Socratic ideation

借 superpowers `brainstorming` 的 Socratic-questioning，但用 Vibe 實際設計過程（ADR-020 federation epic：四輪 strategic discussion + 兩輪外部 adversarial review）萃取出的 heuristic。**先發散提問、再收斂 locked decision、最後外審**——不是一次給答案。

## 何時觸發 / 跳過

- **觸發**：新 ADR、新 component、epic 拆解、`RFC:` 討論、技術選型評估、「該怎麼設計 X」
- **跳過**：code-level debug（→ `engineering:debug`）、PR review（→ `vibe-subagent-review`）、已 locked 的執行（→ 直接做）

## 五個 Vibe 設計提問（發散階段逐項問）

1. **Reuse-over-build**：有沒有現成的開源 / proven 方案？（ADR-020 用 prom-label-proxy 不自寫 endpoint；對齊 lint-adoption-policy「adopt-then-wrap」+「speculative 別造輪子」）
2. **MVP 範圍 vs Future Work**：最小可行版是什麼？哪些**明確 drop 到 Future Work**？（ADR-020：2-tier policy 上、3-tier permission model 下放）
3. **每個決策的 explicit trade-off**：這個選擇換到什麼、犧牲什麼？**寫出來**。（ADR-020：「TTL 4h + 無 server-side revocation list — 明寫換實作簡單」）
4. **Defer-with-trigger，不是 defer-vaguely**：延後的項目給**具體觸發條件**，不是「以後再說」。（ADR-020：「3-tier permission → 等 compliance 客戶觸發」；#442 auto-discovery `wontfix-without-signal`）
5. **Blast-radius / failure mode**：新能力炸掉時影響多大？有沒有護欄？（ADR-020 三件組：concurrency cap / request timeout / series-count cap；對應 `vibe-subagent-review` 的 IaC blast-radius lens）

## 收斂：locked decision 摘要

發散後，把決定寫成 **locked decision 清單**（每條一句 + trade-off），像 ADR-020 §設計討論紀錄那樣。未定的標 open question，不要假裝已決。

## 外部 adversarial review（收斂後）

self-brainstorm 抓內部矛盾；**外部 adversarial review（Gemini / o3）RAG 過 years of postmortem，抓你的盲點**。設計題在 locked decision 後、實作前，走一輪外審（見 `feedback_post_external_review_pass`：take / reframe / reject 三分類，別照單全收——外審也會給杜撰路徑/錯誤前提，套 verify-don't-claim）。ADR-020 兩輪外審補入了 3-layer blast-radius、admission validator 的 label-enrichment 驗證、Metadata API smoke test。

## 反模式（別做）

- 跳過發散直接給單一方案（漏掉 reuse / MVP 選項）
- decision 不寫 trade-off（後人不知為何這樣選、無法重新評估）
- defer 不給 trigger（變成永遠不會回來的 TODO）
- 拿外審意見照單全收（外審會杜撰；先 verify）

## 使用法

設計階段先跑五問發散 → 收斂 locked decision（含 trade-off + defer-trigger）→ 寫進 ADR / RFC → 外審一輪 → 才進實作。對齊 [ADR-020](../../../docs/adr/020-tenant-federation.md) 的實際流程。
