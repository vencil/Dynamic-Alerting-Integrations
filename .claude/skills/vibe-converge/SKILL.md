---
name: vibe-converge
description: 多輪修正的收斂協議 —— decidability gate（開工前先問「這題用手上的證據判得出來嗎」）、跨輪交接契約（只有 verified claim / open question / 已打死方向表跨輪）、面積預算、三條停止規則。Use when 同一個缺陷進入第 2 輪（含）以後的修正、拿到 review finding 要開始修、或出現「每修一輪就冒出新洞、審不完」的感覺。SKIP for 第一輪實作、單檔 doc-only、以及還沒開始寫 code 的設計階段（用 `vibe-brainstorm`）。
---

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

## 三條停止規則（`make converge-status` 會替你判）

1. **CONVERGED** — 連續 2 輪各 0 條新 `verified` finding ⇒ 停。不要為了「再確認一次」開第三輪。
2. **CHANGE-SUBJECT** — 同一受審主體上 `dead-end` ≥ 2 ⇒ ⛔ **禁止第 3 版述詞**，強制走第 0 步換主體或開票。
3. **UNREVIEWED-FIX** — **最後一輪**存在 `status=fixed` 的 finding，而其後**沒有任何輪次宣告受審主體** ⇒ 這輪不算完成，開一輪以那個修法為 `subject`。「已經審過一輪」永遠是指審過**那一版**。
   ⚠️ 這是工具實際跑的述詞，刻意比動機弱：帳本裡**沒有**「本輪主體就是上一輪的修法」這個欄位，所以工具只能用「有沒有下一輪」近似，也**不比較修法與被審主體的面積**（#1431 的 1.6× 是寫規則的理由，不是判定式）。把它做精確需要在 `subject` 加一個 `reviews_round` 欄位——那是可判的，只是今天沒有這個輸入（open question，見 derivation §4）。

（三個門檻各自的依據與已知不確定性：derivation §4。）

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

觀測：`make converge-status SCOPE=dev/<scope>`。

## ⚠️ 誠實邊界（本協議守不到的）

- 帳本是**自陳的**。`make converge-status` 檢查的是**格式**——**不檢查那段 evidence 是不是真的跑過**，`"evidence": "yes"` 會過關。沒有任何機制能從離線文字證明一次執行發生過；這正是 tier 標籤只能靠紀律的原因。加內容述詞去補這個洞，本身就會撞上第 0 步（合法與捏造在離線文字下同構）。
- 停止規則 1（CONVERGED）以**回報的** finding 數為準。一輪沒認真審而回報 0 條，與一輪審完確實 0 條，帳本上長得一樣。這是規則 2、3 存在的理由——它們看的是**主體**與**面積**，不看回報數。
- `LEDGER-GAP` 只檢查輪號連續，**不檢查是否從 1 開始**。從鏈中途才開帳的 scope 合法且靜默。
- 本工具**不進 CI、不進 pre-commit**、不擋任何東西。這是刻意的：#1457 剛刪掉六支「守衛的守衛」，對 review 流程再造一支 gate 會重演同一個病。owner 分類 = 🧠 **skill-advised**（見 [`hook-vs-skill-coverage.md`](../../../docs/internal/hook-vs-skill-coverage.md)）。
- 全部規則由**單一一條修正鏈**導出（n=1）。套到別的情境前先自己量。

## 與既有體系關係

- **[`vibe-subagent-review`](../vibe-subagent-review/SKILL.md)**：管一輪之內怎麼審（lens 路由、finder≠verifier、只報站得住的）。多輪情境下，該 skill 的預設檔位改由本 skill 決定（見該 skill〈預設檔位〉節）。
- **[`vibe-brainstorm`](../vibe-brainstorm/SKILL.md)**：還沒開始寫 code 時用它。第 0 步與 brainstorm 的 blast-radius 提問互補——一個問「這題可判嗎」，一個問「炸掉多大」。
- **`PROGRESS.jsonl`**（`vibe-subagent-review` 長時 agent 協議）：那個是**單一 agent 的存活訊號**；`ROUNDS.jsonl` 是**跨輪的知識交接**。格式慣例相同、用途不重疊，同一個 `dev/<scope>/` 下可並存。
