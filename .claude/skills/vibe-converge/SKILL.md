---
name: vibe-converge
description: 多輪修正的收斂協議 —— decidability gate（開工前先問「這題用手上的證據判得出來嗎」）、跨輪交接契約（只有 verified claim / open question / 已打死方向表跨輪）、面積預算、三條停止規則。Use when 同一個缺陷進入第 2 輪（含）以後的修正、拿到 review finding 要開始修、或出現「每修一輪就冒出新洞、審不完」的感覺。SKIP for 第一輪實作、單檔 doc-only、以及還沒開始寫 code 的設計階段（用 `vibe-brainstorm`）。
---

# vibe-converge — 多輪修正的收斂協議（TRK-360）

`vibe-subagent-review` 決定**一輪**怎麼審；本 skill 決定**輪與輪之間**傳什麼、什麼時候該停、什麼時候該換題目。

## 為什麼存在（本 repo 實測，非通則）

2026-08 對 `_DEFAULTS_ROOTS_MAY_BE_EMPTY` 的修正鏈 [#1411](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1411) → #1415 → #1434 → #1442 → [#1443](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1443) → #1457 燒掉六輪。量出來的四件事：

| 現象 | 量測 | 出處 |
|---|---|---|
| 對一個**資訊上不可判**的問題連下三版述詞 | v1／v2／v3 全掛，#1443 逐字結論「不是判別式寫得不夠好，是資訊上不可能」 | #1443 §已被實測打死的方向 |
| 每輪面積單調成長 | 插入:刪除 = #1415 `2882:67`、#1442 `788:41`、#1457 `1018:19` ⇒ 每輪淨增約 1000 行**沒被任何 lens 掃過**的新面 | `git show --shortstat` |
| 修法 commit 比被審 commit 大且無人審 | 814 行 → 1336 行（**1.6×**），第二輪補審它才找到整輪唯一 Critical | #1431 |
| 作者自審產能為 0 | 「合計 8 位、59+ 條 finding、61 個單點變異，**作者自審 0 條**」 | #1457 PR 正文 |

⛔ **不是**「語言太囉嗦」。同一批文本量過：證據標記（實測/量測/實跑/rc=0/passed/變異…）**1,002 次** vs 推測標記（可能/建議/應該/推測…）**123 次**，8:1 偏向證據，且「似乎／疑似／理論上／或許」**各 0 次**。這個 repo 的用語紀律不是瓶頸——**問錯題目**與**面積成長**才是。

## 第 0 步 — decidability gate（每輪開工前，30 秒）

寫下兩行，缺一不可：

1. **這個判準要回答的問題是什麼？**（一句話，主詞是被檢查的東西）
2. **檢查的當下，手上實際拿得到的證據集合是什麼？**

然後問一句：**在這個證據集合下，「合法情況」與「缺陷情況」長得一樣嗎？**

- 一樣 ⇒ ⛔ **停。不做第 N 版述詞，換受審主體。** 記一筆 `decidability` 進帳本。
- 不一樣 ⇒ 繼續，並把「憑什麼分得出」寫進帳本。

> **#1443 的實例**：問題是「這筆豁免正不正當」，而檢查時唯一的證據是**掩蓋之後的狀態**——加豁免的那一刻那棵樹確實是空的。合法與缺陷同構 ⇒ 三版全掛。#1457 成立不是因為第四版更聰明，是**換了受審主體**（改問「這個檔合不合 schema」，權威是 `docs/schemas/platform-defaults.schema.json`）。

**換受審主體的三個方向**（優先序）：換到有權威 oracle 的那一面（schema／loader／編譯器）→ 換到可重生的量測產物（「這棵 root 今天實際貢獻幾個 key」）→ 開票交 owner 拍板。

## 跨輪交接契約

**只有三類東西跨輪**，其餘一律不跨（可取回：寫 commit SHA / PR 連結，不重貼內文）：

| 跨輪 | 條件 |
|---|---|
| `finding`，tier=`verified` | 附**可重跑指令 + 該次實際輸出**（不是「應該會紅」） |
| `question`，status=`open` | 一句話，且寫明「誰能回答／要什麼證據才能收掉」 |
| `dead-end` | 判準 + **怎麼死的（實測）**。這是負面知識庫，**每輪必帶**，它比 finding 更值錢 |

**證據分級（借 caveman 的 labeled evidence；離線永不自稱 verified）**：

- `verified` — **本輪實際跑過**，帳本裡有指令與輸出。
- `inferred` — 讀 code 推導、沒跑。**本輪內可用，不跨輪**；要跨輪就先把它跑成 `verified`。
- `speculative` — 禁止進帳本。想寫它，代表你該去跑一次。

⛔ **不跨輪**：上一輪的完整 commit body、review 對話、修法過程敘事、「我覺得可能還有」。近 40 顆 commit 的 body 合計 **641,785 字元**（單顆最大 95,733）——那是取回用的檔案，不是交接用的訊息。

## 面積預算

每輪開工時記下受審主體的 `+insertions / -deletions`。

- 插入:刪除 **> 10:1** 且插入 > 300 行、而該輪不是新增測試檔 ⇒ 記一筆 `surface-debt`，並回答一句：**這輪是在換主體，還是在加第 N 版述詞？**
- 是後者 ⇒ 回第 0 步。
- ⚠️ 這是**訊號不是禁令**。新增測試檔天然高比值（#1429 `2508:2`），且**加測試是唯一應該讓面積成長的東西**。

## 三條停止規則（`make converge-status` 會替你判）

1. **CONVERGED** — 連續 2 輪各 0 條新 `verified` finding ⇒ 停。不要為了「再確認一次」開第三輪。
2. **CHANGE-SUBJECT** — 同一受審主體上 `dead-end` ≥ 2 ⇒ ⛔ **禁止第 3 版述詞**，強制走第 0 步換主體或開票。
3. **UNREVIEWED-FIX** — 某輪的修法面積 ≥ 被審面積，而該修法**未在任何後續輪次成為受審主體** ⇒ 這輪不算完成。「已經審過一輪」永遠是指審過**那一版**。

## 帳本：`dev/<scope>/ROUNDS.jsonl`

append-only，一行一筆 JSON（沿用 `PROGRESS.jsonl` 的慣例：不重寫、不刪行、不換檔名）。

```text
{"ts":"<date -u +%FT%TZ>","round":1,"kind":"subject","subject":"<受審主體>","insertions":814,"deletions":12,"reviewer":"blind|self"}
{"ts":"...","round":1,"kind":"decidability","subject":"<同上>","question":"<要回答什麼>","evidence_set":"<檢查時拿得到什麼>","verdict":"decidable|undecidable","note":"<憑什麼分得出／為何同構>"}
{"ts":"...","round":1,"kind":"finding","id":"F1","tier":"verified","status":"open","claim":"<一句話>","evidence":"<指令 => 實際輸出>"}
{"ts":"...","round":1,"kind":"dead-end","subject":"<同上>","claim":"<這版判準是什麼>","evidence":"<怎麼死的，實測>"}
{"ts":"...","round":1,"kind":"question","status":"open","claim":"<問題>","evidence":"<要什麼證據才能收掉>"}
```

`kind=finding` 且 `tier=verified`、以及 `kind=dead-end`，**`evidence` 不得為空**。

觀測：`make converge-status SCOPE=dev/<scope>`。

## ⚠️ 誠實邊界（本協議守不到的）

- 帳本是**自陳的**。`make converge-status` 檢查的是**格式**（verified 有沒有附 evidence、輪次有沒有斷）——**不檢查那段 evidence 是不是真的跑過**。沒有任何機制能從離線文字證明一次執行發生過；這正是 tier 標籤只能靠紀律的原因。
- 本工具**不進 CI、不進 pre-commit**、不擋任何東西。這是刻意的：#1457 剛刪掉六支「守衛的守衛」，對 review 流程再造一支 gate 會重演同一個病。owner 分類 = 🧠 **skill-advised**（見 [`hook-vs-skill-coverage.md`](../../../docs/internal/hook-vs-skill-coverage.md)）。
- 停止規則 1（CONVERGED）以**回報的** finding 數為準。一輪沒認真審而回報 0 條，與一輪審完確實 0 條，帳本上長得一樣。這是規則 2、3 存在的理由——它們看的是**主體**與**面積**，不看回報數。

## 與既有體系關係

- **[`vibe-subagent-review`](../vibe-subagent-review/SKILL.md)**：管一輪之內怎麼審（lens 路由、finder≠verifier、只報站得住的）。多輪情境下，該 skill 的預設檔位改由本 skill 決定（見該 skill〈預設檔位〉節）。
- **[`vibe-brainstorm`](../vibe-brainstorm/SKILL.md)**：還沒開始寫 code 時用它。第 0 步 decidability gate 與 brainstorm 的 blast-radius 提問互補——一個問「這題可判嗎」，一個問「炸掉多大」。
- **`PROGRESS.jsonl`**（同 repo，`vibe-subagent-review` 長時 agent 協議）：那個是**單一 agent 的存活訊號**；`ROUNDS.jsonl` 是**跨輪的知識交接**。兩者格式慣例相同、用途不重疊，同一個 `dev/<scope>/` 下可並存。
