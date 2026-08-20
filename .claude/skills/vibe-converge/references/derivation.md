---
title: "vibe-converge 的推導與出處"
tags: [internal, dx, ai-agent]
audience: [maintainers, ai-agents]
version: v2.9.0
verified-at-version: v2.9.0
lang: zh
---

# vibe-converge — 推導與出處（TRK-360）

> **這份檔案不會自動載入。** `SKILL.md` 只留會改變下一個動作的東西；規則怎麼來的、閾值憑什麼是那個數字、哪些方向已經被打死，全部在這裡，需要時才讀。
>
> 為什麼這樣切：SKILL.md 是**整份載入**的（skill-creator 三層 progressive disclosure：metadata 恆載 → SKILL.md 觸發時整份 → bundled resources 按需；[`skill-system-feature-requests.md` FR-05](../../../../docs/internal/skill-system-feature-requests.md) 也逐字記錄了「Skill tool 一次讀整份 SKILL.md…常只需其中一段」）。出處敘述每次觸發都付費、卻不改變任何一個動作，所以搬到這裡——**搬走不是刪除**，指標留在 SKILL.md 每條規則旁。

## 目錄

- [§1 六輪鏈：本協議的全部證據來源](#1-六輪鏈本協議的全部證據來源)
- [§2 四個現象與各自對應的規則](#2-四個現象與各自對應的規則)
- [§3 被實測否證的原始假設（語言太多且參雜推測）](#3-被實測否證的原始假設語言太多且參雜推測)
- [§4 閾值憑什麼是這些數字](#4-閾值憑什麼是這些數字)
- [§5 已被打死的方向（判準版本墳場）](#5-已被打死的方向判準版本墳場)

## §1 六輪鏈：本協議的全部證據來源

2026-08，對 `check_threshold_reachability.py` 的 `_DEFAULTS_ROOTS_MAY_BE_EMPTY` 豁免清單的修正鏈：

[#1411](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1411) → [#1415](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1415) → [#1434](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1434) → [#1442](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1442) → [#1443](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1443) → [#1457](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1457)

⚠️ **本協議的每一條規則都只由這一條鏈導出**，n = 1。它不是通則，是這個 repo 的一次深度案例；套到別的 repo 前請先自己量。

## §2 四個現象與各自對應的規則

| # | 現象 | 量測 | 出處 | 導出的規則 |
|---|---|---|---|---|
| 1 | 對一個資訊上不可判的問題連寫三版述詞 | v1／v2／v3 全掛 | #1443 §已被實測打死的方向（逐字：「不是判別式寫得不夠好，是**資訊上不可能**」） | **第 0 步 decidability gate** |
| 2 | 每輪面積單調成長 | 插入:刪除 `2882:67`（43:1）／`788:41`（19:1）／`1018:19`（53:1） | `git show --shortstat`，三顆 merge commit | **面積預算** |
| 3 | 修法比被審大且無人審 | 814 行 → **1336 行**（1.6×），第二輪補審它才找到整輪唯一 Critical | #1431 | 停止規則 **UNREVIEWED-FIX** |
| 4 | 作者自審產能為 0 | 逐字：「合計 8 位、59+ 條 finding、61 個單點變異，**作者自審 0 條**」 | #1457 PR 正文 | `vibe-subagent-review`〈預設檔位〉：第 2 輪起改盲審 |

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

重跑方式：`git log -40 --pretty=%B > /tmp/bodies.txt` 後對上表詞彙逐個 `grep -o … | wc -l`。

## §4 閾值憑什麼是這些數字

| 閾值 | 值 | 依據 | 已知的不確定性 |
|---|---|---|---|
| 面積比 | `> 10:1` | 鏈上三輪實測為 43／19／53，最低的一輪仍是 19 ⇒ 10 給了約 2× 餘裕，不會對正常修法誤紅 | 未對「正常修法」的比值分布取樣。10 是餘裕推得的上界，不是分位數 |
| 面積下限 | `> 300` 行 | 低於此的 lopsided 輪次是雜訊；#1429 的 `2508:2` 是純新增測試檔，屬明文合法形狀 | 300 未經取樣校準 |
| CONVERGED | 連續 **2** 輪 0 條 verified | 鏈上沒有任何一輪在前一輪 0 條之後又冒出 verified finding；1 輪太鬆、3 輪就是本協議要消滅的那種確認輪 | n=1，無法區分「2 是對的」與「這條鏈剛好如此」 |
| CHANGE-SUBJECT | dead-end **≥ 2** | v1、v2 死後 v3 仍被寫出來且也死；門檻設在 2 才擋得到 v3 | 若某類問題天生需要三次嘗試，這條會誤擋。目前無反例 |

## §5 已被打死的方向（判準版本墳場）

⛔ 以下都出自 #1443，**是負面知識庫，不要重走**：

| 版本 | 判準 | 怎麼死的（皆為實測） |
|---|---|---|
| v1 | 文件裡任何位置的 `name: ` | 對出貨的 Custom Alerts 範例樹的三種**正確編輯**誤紅，且三者都沒有任何 in-repo 動作可以轉綠 |
| v2 | 非空 mapping、值全為數字 | `all(...)` 這個全稱量詞對「多加一個 key」不封閉 ⇒ 三種實測繞過；同時消掉的現存誤紅是 **0 個** |
| v3 | v2 ＋ schema 合法頂層鍵集合當第二證人 | 兩邊同時中：`_state_*` 是 schema `patternProperties` 的開放前綴（無限集合）⇒ 繞過；同時對正常 recipe 誤紅 |
| B | git provenance（「只有從未產出過閾值才准豁免」） | **出生即壞**：第一顆 commit 就打錯字 ⇒ provenance 判「從未貢獻」⇒ 誤放行 |
| C | digest 指紋 | 指紋在加條目當下產生，證明的是「之後沒再改」，不是「加的時候是乾淨的」——與症狀同病 |

**共同根因**：這五個方向全都在對「當下內容」下述詞，而唯一能拿到的證據是掩蓋之後的狀態。⇒ 這正是第 0 步存在的理由。

## §6 業界對照（#1443 的一手調查摘要）

沒有任何生態系用「從當下內容推測意圖」或「git 歷史 provenance」守豁免的合法性。ESLint 對自家 bulk suppressions 逐字寫過同一個結論（"there's **no reliable way** to determine whether the new violations were introduced recently or already existed"）。共同出路是**換到可量測的一面**：逐條指紋 + 雙向失敗（mypy-baseline / Psalm / PHPStan）、可重生且當原始碼審查的快照（Kubernetes `violation_exceptions.list`）、CI lock 模式（basedpyright）。完整的 20+ 機制調查在 #1443 內文。
