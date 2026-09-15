---
name: vibe-brainstorm
description: 設計階段的 Socratic ideation — 用提問逼出 MVP 範圍、explicit trade-off、defer-with-trigger，加 proposer≠critic 內部對抗 + validate-direction，再走外部 adversarial review。Use when designing a new ADR / new component / epic decomposition / `RFC:` 討論 / 評估技術選型。SKIP for code-level debugging（用 `engineering:debug`）或 PR review（用 `vibe-subagent-review`）——這是「還沒寫 code、在決定要做什麼」的階段。
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

## 設計對抗紀律（收斂後、外審前）

外部外審（下節）抓盲點，但先做這幾條**內部、便宜**的對抗——把散在 memory 的設計紀律 codify 進來（源自 2026-07 security-audit 方法論）：

1. **validate-direction-before-depth（先驗 WHAT 再深挖 HOW）**：對抗式深度是**打磨 HOW、不驗 WHAT**。深挖實作細節前，先用 field-data / 直接證據確認**方向本身對**（要做的是不是對的東西）；別把 core-correctness 當 gold-plating 延後。方向沒驗過，HOW 再漂亮都可能白做。
2. **proposer ≠ critic 自審 pass**：locked decision 後**換帽子當 critic**，對抗式試著**打穿自己的設計**——failure mode 是什麼？最脆的假設是哪條？哪個 trade-off 其實不可接受？blast-radius 護欄真擋得住嗎？打不穿才算收斂。（把現有含糊的「self-brainstorm 抓內部矛盾」結構化；單 agent 便宜。）
3. **design-space coverage-honesty**：locked decision 要列**考慮過但 reject 的替代方案 + 為什麼**，不只寫選中的路——防 first-idea anchoring、也讓後人能重評。

## 升級到多 agent 設計 panel（大題才起；defer-with-trigger）

上面單 agent 五問 + critic 自審適合**多數設計題**。當設計空間**真的很寬**（多個都可行的架構、epic/ADR 級、選錯代價高）時，升級到多 agent panel——reuse [`vibe-security-audit`](../vibe-security-audit/SKILL.md) 的 Workflow harness，換 lens 成設計：

- **平行獨立提案**：多個 agent 各從**不同角度**生一份方案（MVP-first / risk-first / cost-first / user-first），**彼此盲、不 anchor 同一起點** → judge panel 打分 → 從 winner 收斂、把 runner-up 的好點子 graft 進來。
- **proposer ≠ critic 驗證**：每個候選由**不同模型** critic 對抗式打穿；活下來的才進 locked decision。

⚠️ **不要對小設計題起這個**——它貴，是**刻意的升級 tier**；多數題走單 agent 五問 + critic 自審即可（MVP、不 gold-plate 設計流程）。且它**不取代**下節外部 Gemini/o3 外審——內部 panel 抓內部矛盾/盲審，外審 RAG 過 postmortem 抓**內部共有的盲點**，兩者互補。

## 外部 adversarial review（收斂後）

self-brainstorm 抓內部矛盾；**外部 adversarial review（Gemini / o3）RAG 過 years of postmortem，抓你的盲點**。設計題在 locked decision 後、實作前，走一輪外審（見 `feedback_post_external_review_pass`：take / reframe / reject 三分類，別照單全收——外審也會給杜撰路徑/錯誤前提，套 verify-don't-claim）。ADR-020 兩輪外審補入了 3-layer blast-radius、admission validator 的 label-enrichment 驗證、Metadata API smoke test。

## 反模式（別做）

- 跳過發散直接給單一方案（漏掉 reuse / MVP 選項）
- decision 不寫 trade-off（後人不知為何這樣選、無法重新評估）
- defer 不給 trigger（變成永遠不會回來的 TODO）
- 拿外審意見照單全收（外審會杜撰；先 verify）

## 使用法

設計階段先跑五問發散（大題起多 agent 提案 panel）→ 收斂 locked decision（含 trade-off + defer-trigger + reject 的替代）→ **內部 critic 自審 + validate-direction** → 寫進 ADR / RFC → 外審一輪 → 才進實作。對齊 [ADR-020](../../../docs/adr/020-tenant-federation.md) 的實際流程。
