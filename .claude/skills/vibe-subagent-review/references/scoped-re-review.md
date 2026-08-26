<!-- 此檔為產生物，來源 agents/skills/vibe-subagent-review/references/scoped-re-review.md —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# Scoped re-review 骨架（第 2 輪起派 reviewer 用）

〈預設檔位〉說第 2 輪起改派**換 context 的盲審**。這份檔給那一棒的 prompt 骨架。

借 obra/superpowers `subagent-driven-development/re-review-prompt.md`（2026-08-26 逐字取得）。⛔ **不是照抄**：其中「不要重跑測試套件」那條與本 repo 的 always-on 規則 #5 直接衝突，改法與理由見〈與原版的三處分歧〉。

## ⛔ 承重的是那個不延長迴圈的桶，不只是「只看修法」

**本 repo 手上只有一格資料，而且被混淆了。** 這條協議自己那支 PR 的六輪分佈（`dev/stopcond/ROUNDS.jsonl` 實數，合計 20 條）：

| 輪 | lens | finding |
|---|---|---|
| 1 | 自審 | 0 |
| 2 | 盲審，**全體**，兩個互斥 lens | 8 |
| 3 | 盲審，兩顆修法 commit | 4 |
| 4 | 盲審，**只審 rework diff**（scoped） | **1** |
| 5 | 盲審，**全樹**，換主體 lens | 3 |
| 6 | CodeRabbit | 4 |

⚠️ **這格不能拿來宣稱 scoped 有效**：輪 4 的受審 diff 本來就是全鏈最小的（它修的是輪 3 的修法），少的是**範圍**還是 **lens**，這格資料分不出來。

真正沒被試過的是原版多出來的那一格 `Out-of-Scope Observations`，逐字：

> `if you notice an issue entirely outside the fix diff, report it under Out-of-Scope Observations — it does not block this task and does not extend the loop`

⇒ reviewer **照報**（符合本 skill〈Review 紀律〉的音量軸：不要叫它閉嘴），但那些條目**不進本輪的處置清單**、不觸發下一輪。差別在收件端，不在 prompt 端。

⚠️ **這一格在本 repo 從未存在過。** 上表那 20 條沒有任何一條被分類成「範圍外、不延長迴圈」——每一輪的 finding 都餵進了下一輪。所以有這個桶會剩幾條是**未知數**，不是預測。第一次用完請把兩個數字記進帳本：本輪幾條、其中幾條落進 out-of-scope。

## 何時用 / 何時不用

| 情境 | 用不用 |
|---|---|
| 第 2 輪起，且上一輪有具名 finding 要驗收 | ✅ 用這份 |
| 第 1 輪（新實作） | ❌ 走 finder≠verifier 自審 |
| 換了受審主體（走完 `vibe-converge` 第 0 步） | ❌ **那是新的第 1 輪**，用一般盲審——scoped 的前提是「上一輪已全面審過同一個主體」 |

## 骨架（原樣貼進 spawn prompt，方括號代換）

```text
你在重審一輪修法。上一輪的 review 產出了 finding，實作者已嘗試修。
你的工作只有兩件：逐條裁決那些 finding，以及檢查修法本身有沒有弄壞東西。
⛔ 這不是一次全新的 review——全面 review 上一輪已經做過了。

## 受審主體
[SUBJECT]（一句話，主詞是被檢查的東西）

## 待裁決的 finding
[FINDINGS]  ← 逐條照貼上一輪的 verified finding，一條一 bullet

## 已打死的方向（不要再提，提了也不算 finding）
[DEAD_ENDS]  ← 照貼帳本裡的 dead-end：判準 + 怎麼死的

## 修法
base（上一輪 review 看到的那顆）：[FIX_BASE_SHA]
head：[HEAD_SHA]
自己取 diff：git diff --stat [FIX_BASE_SHA]..[HEAD_SHA] 與 git diff [FIX_BASE_SHA]..[HEAD_SHA]

⛔ 你對這棵 checkout 是唯讀的：不得改動 working tree、index、HEAD 或分支狀態。
⛔ 你不 spawn 任何 subagent。這個流程給的 review 席位就是全部；你另外派的
   reviewer 只是複製其中一席、算全價、而且它的裁決不計分。diff 太大就自己分幾
   趟看完，並在報告裡說你分了幾趟。

## 範圍
你的範圍是上面那份 finding 清單，加上修法的 diff。
- 逐條裁決每一個 finding。
- 檢查 diff 本身引入了什麼新問題。
- ⛔ 不要重審 diff 沒碰到的碼。看到完全在 diff 之外的問題，寫進
  〈範圍外觀察〉——它不擋這一輪，也不會開下一輪。

## 測試
實作者宣稱跑過的東西一律當成**未驗證的主張**。你要做的是：確認它有指名是哪幾
支測試、有貼出該次輸出，並拿那些主張去對 diff。⛔ 不要為了確認它而重跑整個
套件。只有在讀碼讀出**具體疑點**、而現有的任何一次執行都答不了時，才自己跑一
支對準那個疑點的測試。你沒跑過的主張，在報告裡標 [未驗證]。

## 輸出格式
你的最後一則訊息就是報告本身：從第一條裁決開始寫。
每一行都是一條裁決、一條帶 file:line 的 finding、或一個你實際跑過的檢查。
⛔ 不要開場白、不要過程敘事、不要「我先看了一下」。

### 逐條裁決
依原順序，每條一行：
- **[finding 一句話]** — ADDRESSED | NOT ADDRESSED，附 file:line 證據。
  ⛔「有嘗試」不算 ADDRESSED：那個具體缺陷必須已經不存在。

### 修法本身的新破壞
修法自己弄壞或引入的東西，附嚴重度（Critical/Important/Minor）與 file:line。
沒有就寫「無」。

### 範圍外觀察
完全在修法 diff 之外的問題。不擋本輪。沒有就寫「無」。

### 本輪裁決
[全部 finding 已處理且無新的 Critical/Important 破壞 | 仍有未關的] — 列出未關的。
```

## 收到之後（收件端才是機制所在）

| 報告的區塊 | 處置 | 進帳本的 `kind` |
|---|---|---|
| 逐條裁決 = NOT ADDRESSED | 進本輪處置清單，走〈收 review〉的 take/reframe/reject | `finding`（`tier` 依你自己驗過沒有） |
| 修法本身的新破壞 | 同上 | 同上 |
| **範圍外觀察** | ⛔ **不進本輪、不開下一輪**。有價值就開票 | `question`，`status=open`，`claim` 寫觀察、`evidence` 寫「要什麼證據才能收掉」 |
| 標 `[未驗證]` 的主張 | 你自己重量一次才寫進出貨物 | — |

⚠️ 用 `question` 是刻意的：`converge_status.py` 對 `question` 只計數不判定（`:302`、`:477`），所以範圍外觀察**不會**經由 `UNREVIEWED-FIX` 把迴圈拉長。**沒有為此新增任何 `kind` 或規則。**

## 與原版的三處分歧

| 原版 | 本 repo | 為什麼 |
|---|---|---|
| `Do not re-run the suite to confirm their report` | 改成「不重跑整套；有具體疑點才跑對準的那一支；沒跑過的標 `[未驗證]`」 | always-on 規則 #5：沒有本輪輸出就不准宣稱通過。原版把驗證責任整個移出 reviewer，本 repo 是移到 **PM 出貨前**那一關，所以 reviewer 必須把「哪些沒驗」標出來讓它可以被接手 |
| 讀預先產好的 `[DIFF_FILE]`、`Do not re-run git commands` | 改成 reviewer 自己下 `git diff` | 本 repo 沒有 `scripts/review-package` 那套前置產物；為了省一次 git 而先造一支打包腳本，會撞〈鷹架准入〉門 2 |
| `[MODEL] — REQUIRED` | 不強制 | 本 repo 沒有 SKILL.md Model Selection 那一節；要換模型是刻意動作，不是每次填的欄位 |

## ⚠️ 誠實邊界

- **這份檔本身就是〈預設檔位〉那個未解前提的一部分**：當前模型的官方指引逐字說 `The same applies to legacy harness scaffolding that adds separate verification steps`、`do not use subagents to verify or double-check your own work`。派一位 scoped re-reviewer 正是那種鷹架。它沒有被關掉，理由與關掉它的方法（同一顆 SHA 自審 vs 一位盲審，比差集）見〈預設檔位〉的引文區塊。
- **out-of-scope 桶的效果未量**（見本檔第二節）。
- 骨架**不擋任何東西**：沒有 hook、沒有 CI、沒有工具會檢查你有沒有用它。與 `vibe-converge` 同一個分類（🧠 skill-advised）。
- 上游原版可能改動。本檔取自 2026-08-26 的 `main`；出現分歧以本檔的三處分歧表為準，不自動跟上游走。
