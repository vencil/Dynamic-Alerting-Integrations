---
name: vibe-converge
description: 多輪修正的收斂協議 —— 觸發時先寫一條可跑的 oracle（寫不出來就換受審主體或停手）、decidability gate、跨輪交接契約（只有 verified claim / open question / 已打死方向表跨輪）、面積預算、輪數上限 5 輪。⛔ 停止條件**不是**「審到零 finding」。Use when 同一個缺陷進入第 2 輪（含）以後的修正、拿到 review finding 要開始修、或出現「每修一輪就冒出新洞、審不完」的感覺。SKIP for 第一輪實作、單檔 doc-only、以及還沒開始寫 code 的設計階段（用 `vibe-brainstorm`）。
---

# vibe-converge — 多輪修正的收斂協議（TRK-360）

`vibe-subagent-review` 決定**一輪**怎麼審；本 skill 決定**輪與輪之間**傳什麼、什麼時候該停、什麼時候該換題目。

> 每條規則的推導、量測、以及**已被打死的判準版本**在 [`references/derivation.md`](references/derivation.md)（不自動載入，需要時才讀）。規則本身在下面，足以照著做。

## 第 0 步之前 — 寫一條 oracle，寫不出來就換主體或停手

**這條鏈憑什麼結束？** 開工前寫一行進帳本，兩個欄位缺一不可：

- `command` — **要跑什麼**（可複製貼上的字串）
- `falsifier` — **哪一個改動會讓它失敗**（程式、產物、設定都算；⛔ 不限於 production code——這條鏈本身可能是文件或流程鏈）

⚠️ oracle 是**整條鏈**的宣告，不屬於任何一輪，所以 **`round` 欄位不要寫**（寫了不會壞，但在從中途開帳的帳本上會造出一個幽靈輪次）。

⚠️ 本 skill 從**第 2 輪**才觸發，所以第一條 oracle 幾乎必然是**回填**的。那是已知的、無法用工具區分的弱點：回填的 oracle 最容易被寫成剛好涵蓋已經做完的事。⇒ 寫的時候問一句：**這條指令會不會對「我還沒做的那半」也說話？**

寫不出 `falsifier` ⇒ 你手上沒有 oracle，這條鏈唯一的終點就是「reviewer 沒話說了」。那不是交付物的性質，是 reviewer 的性質——**去走第 0 步換受審主體，或者不要動工**。若第 0 步判 `undecidable`，記那一筆就是合法的終點，工具不會再要你補 oracle。

⛔ **停止條件不是「審到零 finding」。** 三條實測把它打死：

- 單次 inspection 歷史上只撈到約 **30%** 的既存缺陷（跨研究中位數，Wagner 2006 綜述）
- **61%** 的 review 一個缺陷都沒找到（Cisco，2500 reviews / 3.2M LOC）⇒ 零 finding 是**多數事件**，不具鑑別力
- 這套協議的祖宗（Fagan，以及建在它上面的 Cisco / NASA 流程）用的 exit criteria 逐字是「**已知缺陷已修且已驗證**」，從來不是「找不到新缺陷」。兩句話讀起來很像，是相反的東西

⚠️ 另一面也要知道：被指派去找洞的 reviewer **即使東西是好的也通常會報一些**，而追著每一條修的產物是「多餘的抽象層、防禦性程式碼、以及為不可能發生的情境寫的測試」（Anthropic Claude Code best practices 逐字）。這正是本 repo 量到的形狀。

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

## 停止規則（`make converge-status` 會替你判）

**這條鏈結束，是因為 oracle 過了，不是因為 reviewer 沒話說了。** 以下四條都不數 finding。

0. **EMPTY-LEDGER** — 帳本存在但零筆輪次紀錄 ⇒ ⛔ blocking。**空帳本不是收斂的鏈，是沒記錄的鏈**；若它安靜，「把檔案清掉」就會是滿足以下每一條規則最便宜的方式（實測過：空帳本 rc=0，而誠實記了一輪、只是還沒宣告 oracle 的帳本 rc=1）。
0b. **ORACLE-MISSING** — 整條鏈沒有任何一筆 `kind=oracle` ⇒ ⛔ blocking。理由見上一節。**例外**：帳本裡有 `verdict=undecidable` 時不響——那是本協議自己的終點，再要求一條 oracle 等於要你替一個剛被認證為無解的問題編一個答案。
1. **ROUND-CAP** — 輪數上限 **5**（**5 輪是允許的**，上限是天花板不是最後一輪）。開下一輪的條件是 **oracle 仍然沒過**，不是「reviewer 還有話說」。超過上限 ⇒ 停下來帶**兩個數字**去找 owner：這一輪幾條、其中幾條是**我上一輪修出來的**。
   - ⚠️ **正好在第 5 輪且有未審修法時它也會響，並吸收掉規則 3**。原本兩條會同時響而互相矛盾：規則 3 說「開下一輪去審那個修法」，而開下一輪就撞 ROUND-CAP ⇒ **唯一 rc=0 的出路是把 `status=fixed` 改寫成 `open`**，也就是對「不讓修法逃過審查」這條規則本身說謊。合併成一條之後指令是可執行的（找 owner，發生在工具之外）。
   - ⚠️ **響了就不會再消**，這是設計而非缺陷：這條鏈已經關閉。owner 批准的續作放進**新的 scope、新的帳本、新的 oracle**。
   - 5 這個數字的來源：obra/superpowers 撞到同一個不收斂問題後在 v6.2.0 裝的 five-round circuit breaker；repair-loop 綜述把多數可得增益放在第 1–4 輪（arXiv:2607.05197）。**兩個來源都沒有精確釘住界線，本 repo 也沒有量過**，5 是兩者中較寬鬆的那個。
   - 為什麼要有上限：沒有外部 oracle 時多輪自省**會變差**（GSM8K 95.5% → 91.5% → 89.0%，arXiv:2310.01798），而 5 輪迭代後 critical 漏洞增加 37.6%（IEEE-ISTAS 2025）。
2. **CHANGE-SUBJECT** — 同一受審主體上 `dead-end` ≥ 2 ⇒ ⛔ **禁止第 3 版述詞**，強制走第 0 步換主體或開票。
3. **UNREVIEWED-FIX** — **最後一輪**存在 `status=fixed` 的 finding，而其後**沒有任何輪次宣告受審主體** ⇒ 這輪不算完成，開一輪以那個修法為 `subject`。「已經審過一輪」永遠是指審過**那一版**。
   ⚠️ 這是工具實際跑的述詞，刻意比動機弱：帳本裡**沒有**「本輪主體就是上一輪的修法」這個欄位，所以工具只能用「有沒有下一輪」近似，也**不比較修法與被審主體的面積**（#1431 的 1.6× 是寫規則的理由，不是判定式）。把它做精確需要在 `subject` 加一個 `reviews_round` 欄位——那是可判的，只是今天沒有這個輸入（open question，見 derivation §4）。

（各門檻的依據與已知不確定性：derivation §4。）

## 帳本：`dev/<scope>/ROUNDS.jsonl`

append-only，一行一筆 JSON（沿用 `PROGRESS.jsonl` 的慣例：不重寫、不刪行、不換檔名）。**必須是 UTF-8**——Windows shell 預設寫本地 codepage，工具會對那一行報 `not UTF-8` 並 exit 2。

```text
{"ts":"<date -u +%FT%TZ>","kind":"oracle","command":"<可複製貼上的指令>","falsifier":"<哪一個改動會讓它失敗>"}
{"ts":"<date -u +%FT%TZ>","round":1,"kind":"subject","subject":"<受審主體>","insertions":814,"deletions":12,"reviewer":"blind|self"}
{"ts":"...","round":1,"kind":"decidability","subject":"<同上>","question":"<要回答什麼>","evidence_set":"<檢查時拿得到什麼>","verdict":"decidable|undecidable","note":"<憑什麼分得出／為何同構>"}
{"ts":"...","round":1,"kind":"finding","id":"F1","tier":"verified","status":"open","claim":"<一句話>","evidence":"<指令 => 實際輸出>"}
{"ts":"...","round":1,"kind":"dead-end","subject":"<同上>","claim":"<這版判準是什麼>","evidence":"<怎麼死的，實測>"}
{"ts":"...","round":1,"kind":"question","status":"open","claim":"<問題>","evidence":"<要什麼證據才能收掉>"}
```

`kind=finding` 且 `tier=verified`、以及 `kind=dead-end`，**`evidence` 不得為空**；`kind=oracle` 的 `command` 與 `falsifier` **都不得為空**；`subject` 的 `insertions` / `deletions` 若寫了就必須是非負整數。

觀測：`make converge-status SCOPE=dev/<scope>`。**oracle 無條件印在報告最上面**——一條鏈的終止條件不該是只有違規時才看得到的東西。

## ⚠️ 誠實邊界（本協議守不到的）

- 帳本是**自陳的**。`make converge-status` 檢查的是**格式**——**不檢查那段 evidence 是不是真的跑過**，`"evidence": "yes"` 會過關。沒有任何機制能從離線文字證明一次執行發生過；這正是 tier 標籤只能靠紀律的原因。加內容述詞去補這個洞，本身就會撞上第 0 步（合法與捏造在離線文字下同構）。
- **沒有任何一條停止規則數 finding 了。** 舊的規則 1（CONVERGED，連兩輪 0 條）已刪除：一輪沒認真審而回報 0 條，與一輪審完確實 0 條，帳本上長得一樣，所以它獎勵的是最便宜的那種綠——不要看。現在四條規則各自釘在 oracle 的存在、輪號、主體、面積上，每一條都撐得過一輪不誠實的 review。
- **每條規則最便宜的轉綠方式**（守衛的失敗訊息若指名了比較便宜比較壞的修法，它就會被照做的人拆掉，所以先講。以下每一條都是盲審實際造帳本跑出來的）：
  - `ORACLE-MISSING` — 寫一行看起來合理的假 oracle。**是說謊**。
  - `ROUND-CAP` — **把一條鏈拆成同一個 scope 下的兩支帳本**（6 輪記成 3+3 就安靜）。⛔ **這一條不需要說謊**：每一行紀錄都是真的，而規則依建構是 per-ledger。這是本協議最便宜也最難察覺的繞法，**不防**。
  - `ROUND-CAP`（邊界上）— 把 `status=fixed` 改寫成 `open`。**是說謊**，而且正好對著它要看的那件事。
  - 舊的：把帳本清空。`EMPTY-LEDGER` 關掉了這一格，但**更廣的那點仍然成立——自陳的帳本不會因為多加述詞而變誠實**。
- ⚠️ **`SELF-REVIEW-ZERO` 仍然在數 finding**（它是 advisory 不是停止規則），而且把 `reviewer` 從 `"self"` 改成任何別的字就會消音。**沒有動它**：它建立在 #1457「作者自審 0 條」上，而那筆量測與當前模型的官方指引相反、兩邊都還沒在當前模型上量過（見 `vibe-subagent-review`〈預設檔位〉的未解前提）。
- `LEDGER-GAP` 只檢查輪號連續，**不檢查是否從 1 開始**。從鏈中途才開帳的 scope 合法且靜默。
- 本工具**不進 CI、不進 pre-commit**、不擋任何東西。這是刻意的：#1457 剛刪掉六支「守衛的守衛」，對 review 流程再造一支 gate 會重演同一個病。owner 分類 = 🧠 **skill-advised**（見 [`hook-vs-skill-coverage.md`](../../../docs/internal/hook-vs-skill-coverage.md)）。
- 全部規則由**單一一條修正鏈**導出（n=1）。套到別的情境前先自己量。

## 與既有體系關係

- **[`vibe-subagent-review`](../vibe-subagent-review/SKILL.md)**：管一輪之內怎麼審（lens 路由、finder≠verifier、只報站得住的）。多輪情境下，該 skill 的預設檔位改由本 skill 決定（見該 skill〈預設檔位〉節）。
- **[`vibe-brainstorm`](../vibe-brainstorm/SKILL.md)**：還沒開始寫 code 時用它。第 0 步與 brainstorm 的 blast-radius 提問互補——一個問「這題可判嗎」，一個問「炸掉多大」。
- **`PROGRESS.jsonl`**（`vibe-subagent-review` 長時 agent 協議）：那個是**單一 agent 的存活訊號**；`ROUNDS.jsonl` 是**跨輪的知識交接**。格式慣例相同、用途不重疊，同一個 `dev/<scope>/` 下可並存。
