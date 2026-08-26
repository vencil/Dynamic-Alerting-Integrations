---
name: vibe-converge
description: 多輪修正的收斂協議 —— decidability gate（開工前先問「這題用手上的證據判得出來嗎」）、跨輪交接契約（只有 verified claim / open question / 已打死方向表跨輪）、面積預算、輪數上限 5 輪。⛔ 停止條件**不是**「審到零 finding」，而且本協議**沒有**終止條件。Use when 同一個缺陷進入第 2 輪（含）以後的修正、拿到 review finding 要開始修、或出現「每修一輪就冒出新洞、審不完」的感覺。SKIP for 第一輪實作、單檔 doc-only、以及還沒開始寫 code 的設計階段（用 `vibe-brainstorm`）。
---
<!-- 此檔為產生物，來源 agents/skills/vibe-converge/SKILL.md —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->

# vibe-converge — 多輪修正的收斂協議（TRK-360）

`vibe-subagent-review` 決定**一輪**怎麼審；本 skill 決定**輪與輪之間**傳什麼、什麼時候該停、什麼時候該換題目。

> 每條規則的推導、量測、以及**已被打死的判準版本**在 [`references/derivation.md`](references/derivation.md)（不自動載入，需要時才讀）。規則本身在下面，足以照著做。

## 第 0 步 — decidability gate（每輪開工前，30 秒）

寫下兩行，缺一不可：

1. **這個判準要回答的問題是什麼？**（一句話，主詞是被檢查的東西）
2. **檢查的當下，手上實際拿得到的證據集合是什麼？**

然後問一句：**在這個證據集合下，「合法情況」與「缺陷情況」長得一樣嗎？**

- 一樣 ⇒ ⛔ **停。不做第 N 版述詞，換受審主體。** 記一筆 `decidability` 進帳本。
- 不一樣 ⇒ 繼續，並把「憑什麼分得出」寫進帳本。

> **同構長什麼樣（錨例，用來校準這個判斷）**：問題是「這筆豁免正不正當」，而檢查時唯一的證據是**掩蓋之後的狀態**——加豁免的那一刻，那棵樹確實是空的。合法與缺陷在該證據下同構 ⇒ 三版述詞全掛。破法不是第四版更聰明，是**換受審主體**：改問「這個檔合不合 schema」，權威是一份 schema 檔。（案號與三版死法：derivation §2、§5）

**換受審主體的三個方向**（優先序）：換到有權威 oracle 的那一面（schema／loader／編譯器）→ 換到可重生的量測產物 → 開票交 owner 拍板。

## 跨輪交接契約

**只有三類東西跨輪**，其餘一律不跨（可取回：寫 commit SHA / PR 連結，不重貼內文）：

| 跨輪 | 條件 |
|---|---|
| `finding`，tier=`verified` | 附**可重跑指令 + 該次實際輸出**（不是「應該會紅」） |
| `question`，status=`open` | 一句話，且寫明「誰能回答／要什麼證據才能收掉」 |
| `dead-end` | 判準 + **怎麼死的（實測）**。這是負面知識庫，**每輪必帶**，它比 finding 更值錢 |

**證據分級（離線永不自稱 verified）**：

- `verified` — **本輪實際跑過**，帳本裡有指令與輸出。
- `inferred` — 讀 code 推導、沒跑。**本輪內可用，不跨輪**；要跨輪就先把它跑成 `verified`。
- `speculative` — 禁止進帳本。想寫它，代表你該去跑一次。

⛔ **不跨輪**：上一輪的完整 commit body、review 對話、修法過程敘事、「我覺得可能還有」。那些是**取回用的檔案，不是交接用的訊息**——同一條原則也適用於本 skill 自己，所以出處在 `references/`，不在這裡。

## 面積預算

每輪開工時記下受審主體的 `+insertions / -deletions`。

- 插入:刪除 **> 10:1** 且插入 > 300 行 ⇒ `make converge-status` 記一筆 `surface-debt`，要你回答一句：**這輪是在換主體，還是在加第 N 版述詞？**
- 是後者 ⇒ 回第 0 步。
- ⚠️ 這是**訊號不是禁令**，且工具**不分辨新增的是測試還是判準**——它讀不到檔案類型，帳本裡也沒有這個欄位。「這一輪多出來的面積幾乎都是測試」是**你回答時的合法理由**，不是工具會替你套用的例外（#1429 實測 `2508:2` 就是這種形狀）。（三輪實測比值與閾值餘裕：derivation §2、§4）

## ⛔ 停止條件不是「審到零 finding」——而且本協議沒有終止條件

舊的規則 1（`CONVERGED`：連續 2 輪各 0 條 verified finding）**已刪除**。三條外部證據把它打死（⚠️ 前兩條是量化研究，第三條是**規範性判準**不是量測——不要把三者一起當成量化依據）：

- 單次 inspection 歷史上只撈到約 **30%** 的既存缺陷（跨研究中位數，Wagner 2006 綜述）
- **61%** 的 review 一個缺陷都沒找到（Cisco，2500 reviews / 3.2M LOC）⇒ 零 finding 是**多數事件**，不具鑑別力
- 這套協議的祖宗用的 exit criteria 是「**已知缺陷已修且已驗證**」，不是「找不到新缺陷」。⚠️ **出處分級**：Cisco 案例與 NASA guidebook 一手；**Fagan 1976 原文取不到**（三個位址全失敗），那一半是二手

⚠️ 另一面：被指派去找洞的 reviewer **即使東西是好的也通常會報一些**，而追著每一條修的產物是「多餘的抽象層、防禦性程式碼、以及為不可能發生的情境寫的測試」（Anthropic Claude Code best practices 逐字）。

⛔ **沒有東西取代它。** 下面的 `ROUND-CAP` 是**預算，不是判準**——它說「你用完了」，不說「你做完了」。本協議**沒有終止條件**，`make converge-status` 每次執行都會印一行說明這件事。試過並打死的替代方向見 derivation §4.1。

## 停止規則（`make converge-status` 會替你判）

1. **ROUND-CAP** — 輪數上限 **5**（**5 輪是允許的**，上限是天花板不是最後一輪）。超過 ⇒ 停下來帶**兩個數字**去找 owner：這一輪幾條、其中幾條是**我上一輪修出來的**。
   - ⚠️ **正好在第 5 輪且有未審修法時它也會響，並吸收掉 `UNREVIEWED-FIX`**。否則兩條會互相矛盾：`UNREVIEWED-FIX` 說「開下一輪審那個修法」，而開下一輪就撞 `ROUND-CAP` ⇒ **唯一 rc=0 的出路是把 `status=fixed` 改寫成 `open`**，也就是對「不讓修法逃過審查」這條規則本身說謊。
   - ⚠️ **響了就不會再消**，這是設計：這條鏈已關閉，owner 批准的續作開**新 scope**。⛔ 而工具**分不出**「owner 批准的續作」與「把鏈拆成兩支帳本逃避上限」——後者是它最便宜、**不需說謊**的繞法，**不防**。
   - ⛔ **owner 當下找不到人時，那不是繼續修的理由**：停在原地，把**受審主體 / 仍未關的 finding / 已付出代價的 dead-end** 寫成 handoff 讓下一棒接得住。這個出口被明寫出來，是因為原本的規則只說「去找 owner」——找不到人的人只能自己想辦法，而**最便宜的自己想辦法就是開第二本帳**，也就是上一行那個不防的繞法。（守衛的失敗訊息若指名了比正解更便宜的錯法，錯法就會被照做。）
   - 5 的來源：obra/superpowers v6.2.0 的 five-round circuit breaker；一份 repair-loop 實證評估把多數可得增益放在第 1–4 輪（arXiv:2607.05197，NIER，不是綜述）。**兩者都沒精確釘住界線，本 repo 也沒量過**，5 是較寬鬆的那個。
2. **CHANGE-SUBJECT** — 同一受審主體上 `dead-end` ≥ 2 ⇒ ⛔ **禁止第 3 版述詞**，強制走第 0 步換主體或開票。
   - **消解方式：後續輪次宣告了不同主體**就降級為 advisory（墓碑保留）。⚠️ 它曾經**永不消解**：照訊息做完之後訊息一字不改繼續紅，而帳本 append-only、dead-end 撤不回 ⇒ 誠實記滿兩筆 dead-end 的鏈永遠回不到 rc=0，把「比 finding 更值錢」的那件事變成單向門。
3. **UNREVIEWED-FIX** — **最後一個標了 `status=fixed` 的輪次**之後，**沒有任何輪次宣告受審主體** ⇒ 這輪不算完成，開一輪以那個修法為 `subject`。「已經審過一輪」永遠是指審過**那一版**。
   ⚠️ 不是「最後一輪」：一輪只寫一筆 `question` 曾經可以讓它消音。這仍是刻意比動機弱的述詞——帳本沒有「本輪主體就是上一輪的修法」這個欄位，工具也不比較面積（#1431 的 1.6× 是寫規則的理由，不是判定式）。
4. **LEDGER-GAP** — 輪號不連續 ⇒ blocking。⚠️ 它不檢查是否從 1 開始，因此 **`ROUND-CAP` 數的是帳本裡有審查活動的輪次數，不是真實輪數**——從鏈中途才開帳的人拿到比「上限 5」更寬的額度。兩者是同一個設計決定的兩面。

（各門檻的依據與已知不確定性：derivation §4。）

## 帳本：`dev/<scope>/ROUNDS.jsonl`

append-only，一行一筆 JSON（沿用 `PROGRESS.jsonl` 的慣例：不重寫、不刪行、不換檔名）。**必須是 UTF-8**——Windows shell 預設寫本地 codepage，工具會對那一行報 `not UTF-8` 並 exit 2。

```text
{"ts":"<date -u +%FT%TZ>","round":1,"kind":"subject","subject":"<受審主體>","insertions":814,"deletions":12,"reviewer":"blind|self"}
{"ts":"...","round":1,"kind":"decidability","subject":"<同上>","question":"<要回答什麼>","evidence_set":"<檢查時拿得到什麼>","verdict":"decidable|undecidable","note":"<憑什麼分得出／為何同構>"}
{"ts":"...","round":1,"kind":"finding","id":"F1","tier":"verified","status":"open","claim":"<一句話>","evidence":"<指令 => 實際輸出>"}
{"ts":"...","round":1,"kind":"dead-end","subject":"<同上>","claim":"<這版判準是什麼>","evidence":"<怎麼死的，實測>"}
{"ts":"...","round":1,"kind":"question","status":"open","claim":"<問題>","evidence":"<要什麼證據才能收掉>"}
```

`kind=finding` 且 `tier=verified`、以及 `kind=dead-end`，**`evidence` 不得為空**；`subject` 的 `insertions` / `deletions` 若寫了就必須是非負整數。

觀測：`make converge-status SCOPE=dev/<scope>`（host 無 `make` 時直接跑 `py scripts/tools/dx/converge_status.py --scope dev/<scope>`）。

⚠️ 報告**每次**都印一行 `NOTE: this tool has no terminal condition. A quiet run means no rule fired -- NOT that the work is done.`——因為在這一行存在之前，**一輪什麼都沒找到的盲審與一條真的做完的鏈，輸出與 exit code 逐字相同**。

## ⚠️ 誠實邊界（本協議守不到的）

- 帳本是**自陳的**。`make converge-status` 檢查的是**格式**——**不檢查那段 evidence 是不是真的跑過**，`"evidence": "yes"` 會過關。沒有任何機制能從離線文字證明一次執行發生過；這正是 tier 標籤只能靠紀律的原因。加內容述詞去補這個洞，本身就會撞上第 0 步（合法與捏造在離線文字下同構）。
- **沒有任何一條規則把「finding 少」當成可以停的理由了。** ⚠️ **精確講**：`CHANGE-SUBJECT` 數 dead-end 筆數、`UNREVIEWED-FIX` 鍵在「有幾條 finding 標成 fixed」上，所以「沒有一條規則在數東西」是**假的**。差別在**方向**——少報那兩者會讓規則**更安靜**，而更安靜在那裡代表「鏈還沒完」，不代表「可以收工」。
- **每條規則最便宜的轉綠方式**（守衛的失敗訊息若指名了更便宜更壞的修法，它就會被照做的人拆掉，所以先講）：`ROUND-CAP` ← 把一條鏈拆成同 scope 下兩支帳本（**不需說謊**，最難察覺，不防）；`ROUND-CAP` 邊界 ← 把 `status=fixed` 改寫成 `open`（說謊）；`CHANGE-SUBJECT` ← 不記那筆 dead-end；`UNREVIEWED-FIX` ← 不標 `fixed`；`ROUND-CAP` 的預算 ← 把一輪的 finding 全部記成 `question`（工具只把有 `subject` / `finding` / `dead-end` 的輪次算進預算，見下一條）。
- **`ROUND-CAP` 的預算只由有審查活動的輪次支出**——該輪至少有一筆 `subject` / `finding` / `dead-end`。只帶 `question` 的記帳列不花錢。在這之前它會花掉一輪（實測：5 個真審查輪 rc=0，同樣 5 輪加一列記帳 rc=1），於是**寫下記帳列的人被罰、不寫的人不被罰**。⛔ 換來的最便宜轉綠寫在上一條：把 finding 記成 `question`——那會讓那些 finding 失去 `status`，`UNREVIEWED-FIX` 因此看不到後續的 `fixed`（`converge_status.py` 的 `question` 分支只累加 `open_questions`）。
- ⚠️ **`SELF-REVIEW-ZERO` 仍然在數 finding**（它是 advisory 不是停止規則），而且把 `reviewer` 從 `"self"` 改成任何別的字就會消音。**沒有動它**：見 `vibe-subagent-review`〈預設檔位〉的未解前提。
- `LEDGER-GAP` 只檢查輪號連續，**不檢查是否從 1 開始**。從鏈中途才開帳的 scope 合法且靜默。
- 本工具**不進 CI、不進 pre-commit**、不擋任何東西。這是刻意的：#1457 剛刪掉六支「守衛的守衛」，對 review 流程再造一支 gate 會重演同一個病。owner 分類 = 🧠 **skill-advised**（見 [`hook-vs-skill-coverage.md`](../../../docs/internal/hook-vs-skill-coverage.md)）。
- 全部規則由**單一一條修正鏈**導出（n=1）。套到別的情境前先自己量。

## 與既有體系關係

- **[`vibe-subagent-review`](../vibe-subagent-review/SKILL.md)**：管一輪之內怎麼審（lens 路由、finder≠verifier、只報站得住的）。多輪情境下，該 skill 的預設檔位改由本 skill 決定（見該 skill〈預設檔位〉節）。
- **[`vibe-brainstorm`](../vibe-brainstorm/SKILL.md)**：還沒開始寫 code 時用它。第 0 步與 brainstorm 的 blast-radius 提問互補——一個問「這題可判嗎」，一個問「炸掉多大」。
- **`PROGRESS.jsonl`**（`vibe-subagent-review` 長時 agent 協議）：那個是**單一 agent 的存活訊號**；`ROUNDS.jsonl` 是**跨輪的知識交接**。格式慣例相同、用途不重疊，同一個 `dev/<scope>/` 下可並存。
