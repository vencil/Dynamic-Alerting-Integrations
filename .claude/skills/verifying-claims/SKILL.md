---
name: verifying-claims
description: Routes a claim the agent is about to make to the agent-rulebook rule that governs it. Use right before writing any of these — "tests pass / lint clean / fixed / CI green", "nothing covers X", "this change adds detection", any count or "N places across the repo", "fixed file:line" for a review finding, a new guard predicate, "this was reviewed", or when a fix round keeps producing new findings. Also use when deciding whether a green CI can be trusted or whether to relax a failing assertion. SKIP for design opinions (how something should be built may be asserted without evidence) and for questions that only read code.
---
<!-- 此檔為產生物，來源 .agents/skills/verifying-claims/SKILL.md —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->

# verifying-claims — 宣稱之前先走哪一條

完整規則（觸發時刻／約束／機械判準）在 [`agent-rulebook.md`](../../../docs/internal/agent-rulebook.md)，為什麼與已知失敗形狀在 [`agent-rulebook-shapes.md`](../../../docs/internal/agent-rulebook-shapes.md)。本檔只做路由：找到那一條，做最小動作。

## 路由表（判準是你站在哪個時點，不是缺陷屬哪一類）

| 你正要做的事 | 走 | 最小動作 |
|---|---|---|
| 寫下「通過／乾淨／修好／綠了」 | CLAUDE.md 不可協商 #5 | 貼本輪跑過的指令與輸出；跑不了就寫一行 `[未驗] <宣稱>`＋擋住它的那件事 |
| 寫下一句關於現況的肯定句 | D-09 | 先 grep，附 `file:line`；設計意見可裸斷言 |
| 寫下「沒有 X 涵蓋」 | D-03 | 搜能力面（不是症狀面），帶一個必響對照組 |
| 主張「本次買到偵測力」 | D-02 | 還原整支改動、同情境重跑，差集才是收穫 |
| 看到綠燈想信它 | D-04 | 讀完所有能讓 gate 不發生的機制：path filter、未註冊、依賴缺、stdin 空 |
| 設計或修改守衛述詞 | D-05 | 問「有沒有能從兩個來源各自推導再比對的性質」；先量誤紅面 |
| 拿到 finding 正要改被點名那一行 | D-06 | 改寫成「這一類全 repo N 處」；說不出 N 就是還沒掃 |
| 產出任何要寫下來的數字 | D-07 | parse 不 grep；至少三種鍵；母體未截斷；含 CJK 的 pattern 走 Python `re` |
| 量測與模型衝突，想修量測 | D-08 | 先證明①量測有偏差②偏差解釋得了這個不一致 |
| 多輪 diff 審查後要宣布「審過了」 | D-10 | 收尾補一次整份檔案的審查 |
| 這一輪 finding 多半來自上一輪修法 | D-01 | 減法：砍加固，砍後對倖存者各跑一格變異 |

D-01 與 D-04／D-05 衝突時的仲裁：**守衛值得留，當且僅當它的靜默失效會讓它所守的那一類缺陷無聲通過。**

## 與 `vibe-converge` 的分工

本 skill 管「這一句話能不能寫」；輪與輪之間傳什麼、何時停、何時換受審主體由 [`vibe-converge`](../vibe-converge/SKILL.md) 管。
