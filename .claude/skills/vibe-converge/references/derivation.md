---
title: "vibe-converge 的推導與出處"
tags: [internal, dx, ai-agent]
audience: [maintainers, ai-agents]
version: v2.9.0
verified-at-version: v2.9.0
lang: zh
---
<!-- 此檔為產生物，來源 agents/skills/vibe-converge/references/derivation.md —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->

# vibe-converge — 推導與出處（TRK-360）

> **這份檔案不會自動載入。** `SKILL.md` 只留會改變下一個動作的東西；規則怎麼來的、閾值憑什麼是那個數字、哪些方向已經被打死，全部在這裡，需要時才讀。
>
> 為什麼這樣切：SKILL.md 是**整份載入**的（skill-creator 三層 progressive disclosure：metadata 恆載 → SKILL.md 觸發時整份 → bundled resources 按需；[`skill-system-feature-requests.md` FR-05](../../../../docs/internal/skill-system-feature-requests.md) 也逐字記錄了「Skill tool 一次讀整份 SKILL.md…常只需其中一段」）。出處敘述每次觸發都付費、卻不改變任何一個動作，所以搬到這裡——**搬走不是刪除**，指標留在 SKILL.md 每條規則旁。

## 目錄

- [§1 六輪鏈：本協議的全部證據來源](#1-六輪鏈本協議的全部證據來源)
- [§2 五個現象與各自對應的規則](#2-五個現象與各自對應的規則)
- [§3 被實測否證的原始假設（語言太多且參雜推測）](#3-被實測否證的原始假設語言太多且參雜推測)
- [§4 閾值憑什麼是這些數字](#4-閾值憑什麼是這些數字)
- [§5 已被打死的方向（判準版本墳場）](#5-已被打死的方向判準版本墳場)
- [§6 業界對照（#1443 的一手調查摘要）](#6-業界對照1443-的一手調查摘要)

## §1 六輪鏈：本協議的全部證據來源

2026-08，對 `check_threshold_reachability.py` 的 `_DEFAULTS_ROOTS_MAY_BE_EMPTY` 豁免清單的修正鏈：

[#1411](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1411) → [#1415](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1415) → [#1434](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1434) → [#1442](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1442) → [#1443](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1443) → [#1457](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1457)

⚠️ **本協議的每一條規則都只由這一條鏈導出**，n = 1。它不是通則，是這個 repo 的一次深度案例；套到別的 repo 前請先自己量。

## §2 五個現象與各自對應的規則

| # | 現象 | 量測 | 出處 | 導出的規則 |
|---|---|---|---|---|
| 1 | 對一個資訊上不可判的問題連寫三版述詞 | v1／v2／v3 全掛 | #1443 §已被實測打死的方向（逐字：「不是判別式寫得不夠好，是**資訊上不可能**」） | **第 0 步 decidability gate** |
| 2 | 每輪面積單調成長 | 插入:刪除 `2882:67`（43:1）／`788:41`（19:1）／`1018:19`（53:1） | `git show --shortstat`，三顆 merge commit | **面積預算** |
| 3 | 修法比被審大且無人審 | 814 行 → **1336 行**（1.6×），第二輪補審它才找到整輪唯一 Critical | #1431 | 停止規則 **UNREVIEWED-FIX** |
| 4 | 作者自審產能為 0 | 逐字：「合計 8 位、59+ 條 finding、61 個單點變異，**作者自審 0 條**」 | #1457 PR 正文 | `vibe-subagent-review`〈預設檔位〉：第 2 輪起改盲審 |
| 5 | **迴圈的產出打在鷹架上，不在交付物上** | 最近 60 顆 first-parent commit：self-serving（dx/lint/ci/docs/test）**47** vs product **13**（conventional-commit type/scope，0 unparsed）；2026-08 議題開 **171** 關 **47**（淨 +124），其中仍 OPEN 的 133 張有 **≥59%** 可追溯到某支 PR 或自陳續辦（兩把結構不同的鍵取聯集，交集僅 25 ⇒ 59% 是下界） | commit 組成量在 `bf16d303`（2026-08-25）；議題數綁查詢日期 2026-08-26、不綁 SHA | 停止規則 **ORACLE-MISSING** ＋ **ROUND-CAP** |

**現象 5 的界線**：這三個數量的是**組成與速率，不是因果**。它們與「迴圈自我指涉」一致，但單憑它們證不了因果——會推翻的解釋是「這段期間的 dx 比重是 owner 刻意排的優先序」（2026-08-26 owner 逐字答覆：不是，「自己長出來的」）。另外 cohort 關閉率（100%→97.5%→79.5%→48.8%→22.2%）**被年齡混淆**，不列為證據。

**現象 4 的未解狀態（2026-08-26）**：那個「自審 0 條」是在**更早的模型世代**上量的。當前模型的官方指引方向相反——逐字要求移除「use a subagent to verify」這類明示驗證指令，並點名 `legacy harness scaffolding that adds separate verification steps`。**兩邊都沒有在當前模型上被量過**，因此本次只把輪數收上限，**不動「審的人要換」這條**。關掉它的方式是側量：同一支 PR、同一顆 SHA，自審一次與盲審一次，比兩個 finding 集合。

**現象 1 的關鍵細節**（這是 decidability gate 的定義性案例）：要問的是「這筆豁免正不正當」，而檢查的當下唯一能拿到的證據是**掩蓋之後的狀態**——加豁免的那一刻，那棵樹確實是空的。合法情況與缺陷情況在該證據集合下同構。#1457 成立不是因為第四版述詞更聰明，是**換了受審主體**：改問「這個檔合不合 schema」，權威是 `docs/schemas/platform-defaults.schema.json`。

## §3 被實測否證的原始假設（語言太多且參雜推測）

開這個協議時的原始假設是「跨輪交互詞語太多**且參雜推測的說法**」。前半成立、**後半被實測推翻**，所以本協議不含任何「話要精簡」條目。

近 40 顆 commit body（合計 **641,785 字元**，單顆最大 `c33508d` 的 95,733）逐詞計數：

| 類別 | 次數 |
|---|---|
| 證據標記（實測 382／passed 148／變異 132／量測 90／rc=0 69／逐條 41／實跑 49／逐字 37／反例 29／矩陣 14／證人 11） | **1,002** |
| 推測標記（可能 53／建議 41／猜 11／應該 8／推測 3／看似 3／傾向 2／大概 2） | **123** |
| 似乎／疑似／理論上／或許 | **各 0** |

⇒ 8:1 偏向證據。**用語紀律不是瓶頸**，因此跨輪交接契約管的是「哪些東西有資格跨輪」，不是措辭。

⚠️ **本節三個數字錨在 `0d5e50df`（#1457）＝「當時的最近 40 顆 first-parent」，檔案原本沒寫這個錨。** 單位是 **bytes 不是字元**（`wc -c`；中文語料下兩者差約 3×）。2026-08-26 在 `e9b3ac44` 上照同一條配方重量：總量 **805,731 bytes**（原記 641,785）、`實測` **468**（原記 382）、`可能` **50**（原記 53）。

⛔ **最後一列已被證偽**：表格逐字寫「似乎／疑似／理論上／或許 **各 0**」，今天實測是 **1 / 1 / 0 / 1**。本節的結論（8:1 偏向證據 ⇒ 用語紀律不是瓶頸）方向仍成立，**但它現在靠的是前兩列，不是三列**。⇒ 依本 skill 自己的規則（不可重新推導或已證偽的數字不留在散文裡），這一列保留為 **errata 形式**而非事實——刪掉它會把「這一族數字會腐爛」這件事一起抹掉，而那正是它現在最有價值的地方。

⚠️ **重跑配方的可攜性**：原文寫 `> /tmp/bodies.txt`，在本 repo 的 Windows host 上 `/tmp` 會落到 `C:\tmp`，且直譯器是 `py` 不是 `python3`。

重跑方式：`git log -40 --pretty=%B > /tmp/bodies.txt` 後對上表詞彙逐個 `grep -o … | wc -l`。

## §4 閾值憑什麼是這些數字

| 閾值 | 值 | 依據 | 已知的不確定性 |
|---|---|---|---|
| 面積比 | `> 10:1` | 鏈上三輪實測為 43／19／53，最低的一輪仍是 19 ⇒ 10 給了約 2× 餘裕，不會對正常修法誤紅 | 未對「正常修法」的比值分布取樣。10 是餘裕推得的上界，不是分位數 |
| 面積下限 | `> 300` 行 | 低於此的 lopsided 輪次是雜訊；#1429 的 `2508:2` 是純新增測試檔，屬明文合法形狀 | 300 未經取樣校準 |
| ~~CONVERGED~~ | ~~連續 **2** 輪 0 條 verified~~ | **已刪除（2026-08-26）**，理由見下方 §4.1 | — |
| ROUND-CAP | **5** 輪 | obra/superpowers v6.2.0 在同一個不收斂問題上裝的 five-round circuit breaker；一份 repair-loop 的實證評估把多數可得增益放在第 1–4 輪（arXiv:2607.05197，NIER，不是綜述） | **兩個來源都沒有精確釘住界線，本 repo 沒有量過**。5 是兩者中較寬鬆的那個——刻意選寬，因為選窄的代價（漏報）比選寬的代價（多一輪）難察覺 |
| ORACLE-MISSING | 存在性（≥1 筆） | 一條鏈若沒有外部 oracle，終止條件就退回「reviewer 沒話說」 | 只驗**存在**與**形狀**，不驗那條指令真的跑過、也不驗它真的測到東西。這是自陳帳本的固有上限 |
| EMPTY-LEDGER | 存在性（≥1 筆輪次） | 空帳本若安靜，「把檔案清掉」就是滿足其他每一條規則最便宜的方式（實測：空帳本 rc=0、訠實記了一輪卻未宣告 oracle 的帳本 rc=1） | 只驗「有輪次」不驗「輪次有內容」：一筆零內容紀錄即滿足。另外它關的是「清空」不是「刪除」 |
| LEDGER-GAP | 輪號需連續 | 缺一輪就是那一輪的 finding 沒跨到下一輪 | 不檢查是否從 1 開始，因此 **`ROUND-CAP` 數的是帳本 span 不是真實輪數**；兩者是同一個設計決定的兩面 |
| CHANGE-SUBJECT | dead-end **≥ 2** | v1、v2 死後 v3 仍被寫出來且也死；門檻設在 2 才擋得到 v3 | 若某類問題天生需要三次嘗試，這條會誤擋。目前無反例 |

### §4.1 為什麼刪掉 CONVERGED（2026-08-26）

原本的理由（「鏈上沒有任何一輪在前一輪 0 條之後又冒出 verified finding」）**n=1**，而三條外部證據指向相反方向：

| 證據 | 內容 |
|---|---|
| Wagner 2006 綜述（defect-detection techniques） | 單次 inspection 的效果跨研究 mean **34.14%** / median **30%**（range 8.5–92.7%）⇒ 一輪零 finding 與「零缺陷」在統計上相距甚遠 |
| Cisco 案例（2500 reviews / 3.2M LOC，Cohen 等《Best Kept Secrets of Peer Code Review》） | **61%** 的 review 找到零缺陷 ⇒ 零 finding 是多數事件，不具鑑別力 |
| Fagan / Cisco / NASA 的 exit criteria | 用的是「**已知缺陷已修且已驗證**」，不是「找不到新缺陷」。前者可判定、後者是 reviewer 的性質 |

⭐ 另一個獨立面：Anthropic Claude Code best practices 逐字警告 `A reviewer prompted to find gaps will usually report some, even when the work is sound`，並點名追每一條 finding 的三個產物是 `extra abstraction layers, defensive code, and tests for cases that can't happen`。本 repo 量到的形狀與這三個逐字對應（見 §2 現象 5）。

⛔ **刪除而不是改寫**：改寫（例如「連 3 輪」）只是把同一個代理指標的門檻推得更極端，而 Goodhart 的 regressional 形式說明——proxy 與目標的差距**必然**在尾端放大（Manheim & Garrabrant, arXiv:1803.04585）。指標本身要換，不是門檻。

⚠️ **反方向的證據也記在這裡，它沒有被推翻**：Porter et al.（TOSEM 1998）與 Rigby & Bird（FSE 2013）量到「2 位 reviewer ≈ 4 位」、加人只有 minimal increase。**這支持「該停」**——錯的只是拿「零 finding」當那個停手訊號。ROUND-CAP 是照這條設的。

## §5 已被打死的方向（判準版本墳場）

⛔ 以下都出自 #1443，**是負面知識庫，不要重走**：

| 版本 | 判準 | 怎麼死的（皆為實測） |
|---|---|---|
| v1 | 文件裡任何位置的 `name:`（後接一個空格） | 對出貨的 Custom Alerts 範例樹的三種**正確編輯**誤紅，且三者都沒有任何 in-repo 動作可以轉綠 |
| v2 | 非空 mapping、值全為數字 | `all(...)` 這個全稱量詞對「多加一個 key」不封閉 ⇒ 三種實測繞過；同時消掉的現存誤紅是 **0 個** |
| v3 | v2 ＋ schema 合法頂層鍵集合當第二證人 | 兩邊同時中：`_state_*` 是 schema `patternProperties` 的開放前綴（無限集合）⇒ 繞過；同時對正常 recipe 誤紅 |
| B | git provenance（「只有從未產出過閾值才准豁免」） | **出生即壞**：第一顆 commit 就打錯字 ⇒ provenance 判「從未貢獻」⇒ 誤放行 |
| C | digest 指紋 | 指紋在加條目當下產生，證明的是「之後沒再改」，不是「加的時候是乾淨的」——與症狀同病 |

**共同根因**：這五個方向全都在對「當下內容」下述詞，而唯一能拿到的證據是掩蓋之後的狀態。⇒ 這正是第 0 步存在的理由。

## §6 業界對照（#1443 的一手調查摘要）

沒有任何生態系用「從當下內容推測意圖」或「git 歷史 provenance」守豁免的合法性。ESLint 對自家 bulk suppressions 逐字寫過同一個結論（"there's **no reliable way** to determine whether the new violations were introduced recently or already existed"）。共同出路是**換到可量測的一面**：逐條指紋 + 雙向失敗（mypy-baseline / Psalm / PHPStan）、可重生且當原始碼審查的快照（Kubernetes `violation_exceptions.list`）、CI lock 模式（basedpyright）。完整的 20+ 機制調查在 #1443 內文。
