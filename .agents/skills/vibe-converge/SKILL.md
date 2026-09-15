---
name: vibe-converge
description: 多輪修正的收斂協議 —— decidability gate（開工前先問「這題用手上的證據判得出來嗎」）、跨輪交接契約（只有 verified claim / open question / 已打死方向表跨輪）、面積預算、輪數上限 5 輪。⛔ 停止條件**不是**「審到零 finding」，而且本協議**沒有**終止條件。Use when 同一個缺陷進入第 2 輪（含）以後的修正、拿到 review finding 要開始修、或出現「每修一輪就冒出新洞、審不完」的感覺。SKIP for 第一輪實作、單檔 doc-only、以及還沒開始寫 code 的設計階段（用 `vibe-brainstorm`）。
---

# vibe-converge

`vibe-subagent-review` 決定一輪怎麼審；本 skill 決定輪與輪之間傳什麼、什麼時候該停、什麼時候該換題目。規則的推導、量測與已打死的版本在 [`references/derivation.md`](references/derivation.md)；四條停止規則的細則、各自怎麼消解、以及工具守不到的邊界在 [`references/boundaries.md`](references/boundaries.md)（規則響了、或想知道某條怎麼轉綠時讀）。

## 第 0 步：decidability gate（每輪開工前）

寫下兩行：這個判準要回答的問題是什麼；檢查當下手上實際拿得到的證據集合是什麼。然後問：**在這個證據集合下，合法情況與缺陷情況長得一樣嗎？** 一樣 ⇒ 停，不寫第 N 版述詞，換受審主體，記一筆 `decidability`。換主體的優先序：換到有權威 oracle 的那一面（schema／loader／編譯器）→ 換到可重生的量測產物 → 開票交 owner 拍板。

## 跨輪只傳三類

| 跨輪 | 條件 |
|---|---|
| `finding`，tier=`verified` | 附可重跑指令 + 該次實際輸出 |
| `question`，status=`open` | 一句話，寫明誰能回答／要什麼證據才能收掉 |
| `dead-end` | 判準 + 怎麼死的（實測）。每輪必帶 |

`verified`＝本輪跑過、帳本有指令與輸出；`inferred`＝讀碼推導，本輪內可用、⛔ 不跨輪；`speculative`（「我覺得可能還有」）⛔ 禁止進帳本。上一輪的 commit body、review 對話、修法敘事不跨輪（可取回：寫 SHA / PR 連結）。

## 停止規則（`make converge-status SCOPE=dev/<scope>` 會判）

1. **ROUND-CAP**：輪數上限 5（5 輪是允許的，上限是天花板不是最後一輪）。超過 ⇒ 帶兩個數字去找 owner：這一輪幾條、其中幾條是我上一輪修出來的。找不到人 ⇒ 停在原地寫 handoff（受審主體／未關 finding／dead-end），不開第二本帳。
2. **CHANGE-SUBJECT**：同一受審主體 `dead-end` ≥ 2 ⇒ 禁止第 3 版述詞，回第 0 步。
3. **UNREVIEWED-FIX**：最後一個 `status=fixed` 之後沒有任何輪次宣告受審主體 ⇒ 開一輪以那個修法為 subject。
4. **LEDGER-GAP**：輪號不連續 ⇒ blocking。

面積預算：每輪記受審主體的 `+insertions / -deletions`；插入:刪除 > 10:1 且插入 > 300 行時工具記 `surface-debt`，你要回答「這輪是在換主體，還是在加第 N 版述詞」。

⛔ 本協議沒有終止條件。ROUND-CAP 是預算不是判準；「審到零 finding」不是停止條件，工具每次執行都印一行說明這件事。

## 帳本 `dev/<scope>/ROUNDS.jsonl`

append-only、一行一筆、必須是 UTF-8（Windows shell 預設寫本地 codepage，工具會對那一行報 `not UTF-8` 並 exit 2）；`subject` 的 `insertions` / `deletions` 若寫了必須是非負整數：

```text
{"ts":"<date -u +%FT%TZ>","round":1,"kind":"subject","subject":"<受審主體>","insertions":814,"deletions":12,"reviewer":"blind|self"}
{"ts":"...","round":1,"kind":"decidability","subject":"<同上>","question":"<要回答什麼>","evidence_set":"<檢查時拿得到什麼>","verdict":"decidable|undecidable","note":"<憑什麼分得出／為何同構>"}
{"ts":"...","round":1,"kind":"finding","id":"F1","tier":"verified","status":"open","claim":"<一句話>","evidence":"<指令 => 實際輸出>"}
{"ts":"...","round":1,"kind":"dead-end","subject":"<同上>","claim":"<這版判準是什麼>","evidence":"<怎麼死的，實測>"}
{"ts":"...","round":1,"kind":"question","status":"open","claim":"<問題>","evidence":"<要什麼證據才能收掉>"}
```

`finding`（verified）與 `dead-end` 的 `evidence` 不得為空。host 無 `make` 時跑 `py scripts/tools/dx/converge_status.py --scope dev/<scope>`（或 `python`）。帳本是自陳的：工具只驗格式，不驗 evidence 是否真的跑過。
