# vibe-converge：規則的細節、來源與工具守不到的邊界（從 SKILL.md 搬出，逐字；不自動載入）

## 第 0 步的錨例

**同構長什麼樣（用來校準這個判斷）**：問題是「這筆豁免正不正當」，而檢查時唯一的證據是**掩蓋之後的狀態**——加豁免的那一刻，那棵樹確實是空的。合法與缺陷在該證據下同構 ⇒ 三版述詞全掛。破法不是第四版更聰明，是**換受審主體**：改問「這個檔合不合 schema」，權威是一份 schema 檔。（案號與三版死法：derivation §2、§5）

## 面積預算的細節

- ⚠️ 這是**訊號不是禁令**，且工具**不分辨新增的是測試還是判準**——它讀不到檔案類型，帳本裡也沒有這個欄位。「這一輪多出來的面積幾乎都是測試」是**你回答時的合法理由**，不是工具會替你套用的例外（#1429 實測 `2508:2` 就是這種形狀）。（三輪實測比值與閾值餘裕：derivation §2、§4）

## 停止條件不是「審到零 finding」——證據

舊的規則 1（`CONVERGED`：連續 2 輪各 0 條 verified finding）**已刪除**。三條外部證據把它打死（⚠️ 前兩條是量化研究，第三條是**規範性判準**不是量測——不要把三者一起當成量化依據）：

- 單次 inspection 歷史上只撈到約 **30%** 的既存缺陷（跨研究中位數，Wagner 2006 綜述）
- **61%** 的 review 一個缺陷都沒找到（Cisco，2500 reviews / 3.2M LOC）⇒ 零 finding 是**多數事件**，不具鑑別力
- 這套協議的祖宗用的 exit criteria 是「**已知缺陷已修且已驗證**」，不是「找不到新缺陷」。⚠️ **出處分級**：Cisco 案例與 NASA guidebook 一手；**Fagan 1976 原文取不到**（三個位址全失敗），那一半是二手

⚠️ 另一面：被指派去找洞的 reviewer **即使東西是好的也通常會報一些**，而追著每一條修的產物是「多餘的抽象層、防禦性程式碼、以及為不可能發生的情境寫的測試」（Anthropic Claude Code best practices 逐字）。

⛔ **沒有東西取代它。** `ROUND-CAP` 是**預算，不是判準**——它說「你用完了」，不說「你做完了」。試過並打死的替代方向見 derivation §4.1。

## 停止規則的細節

1. **ROUND-CAP**
   - ⚠️ **正好在第 5 輪且有未審修法時它也會響，並吸收掉 `UNREVIEWED-FIX`**。否則兩條會互相矛盾：`UNREVIEWED-FIX` 說「開下一輪審那個修法」，而開下一輪就撞 `ROUND-CAP` ⇒ **唯一 rc=0 的出路是把 `status=fixed` 改寫成 `open`**，也就是對「不讓修法逃過審查」這條規則本身說謊。
   - ⚠️ **響了就不會再消**，這是設計：這條鏈已關閉，owner 批准的續作開**新 scope**。⛔ 而工具**分不出**「owner 批准的續作」與「把鏈拆成兩支帳本逃避上限」——後者是它最便宜、**不需說謊**的繞法，**不防**。
   - ⛔ **owner 當下找不到人時，那不是繼續修的理由**：停在原地，把**受審主體 / 仍未關的 finding / 已付出代價的 dead-end** 寫成 handoff 讓下一棒接得住。這個出口被明寫出來，是因為原本的規則只說「去找 owner」——找不到人的人只能自己想辦法，而**最便宜的自己想辦法就是開第二本帳**。（守衛的失敗訊息若指名了比正解更便宜的錯法，錯法就會被照做。）
   - 5 的來源：obra/superpowers v6.2.0 的 five-round circuit breaker；一份 repair-loop 實證評估把多數可得增益放在第 1–4 輪（arXiv:2607.05197，NIER，不是綜述）。**兩者都沒精確釘住界線，本 repo 也沒量過**，5 是較寬鬆的那個。
2. **CHANGE-SUBJECT** — **消解方式：後續輪次宣告了不同主體**就降級為 advisory（墓碑保留）。⚠️ 它曾經**永不消解**：照訊息做完之後訊息一字不改繼續紅，而帳本 append-only、dead-end 撤不回 ⇒ 誠實記滿兩筆 dead-end 的鏈永遠回不到 rc=0，把「比 finding 更值錢」的那件事變成單向門。
3. **UNREVIEWED-FIX** — ⚠️ 不是「最後一輪」：一輪只寫一筆 `question` 曾經可以讓它消音。這仍是刻意比動機弱的述詞——帳本沒有「本輪主體就是上一輪的修法」這個欄位，工具也不比較面積（#1431 的 1.6× 是寫規則的理由，不是判定式）。
4. **LEDGER-GAP** — ⚠️ 它不檢查是否從 1 開始，因此 **`ROUND-CAP` 數的是帳本裡有審查活動的輪次數，不是真實輪數**——從鏈中途才開帳的人拿到比「上限 5」更寬的額度。兩者是同一個設計決定的兩面。

（各門檻的依據與已知不確定性：derivation §4。）

## 工具每次都印的那一行

`NOTE: this tool has no terminal condition. A quiet run means no rule fired -- NOT that the work is done.`——因為在這一行存在之前，**一輪什麼都沒找到的盲審與一條真的做完的鏈，輸出與 exit code 逐字相同**。

## ⚠️ 誠實邊界（本協議守不到的）

- 帳本是**自陳的**。`make converge-status` 檢查的是**格式**——**不檢查那段 evidence 是不是真的跑過**，`"evidence": "yes"` 會過關。沒有任何機制能從離線文字證明一次執行發生過；這正是 tier 標籤只能靠紀律的原因。加內容述詞去補這個洞，本身就會撞上第 0 步（合法與捏造在離線文字下同構）。
- **沒有任何一條規則把「finding 少」當成可以停的理由了。** ⚠️ **精確講**：`CHANGE-SUBJECT` 數 dead-end 筆數、`UNREVIEWED-FIX` 鍵在「有幾條 finding 標成 fixed」上，所以「沒有一條規則在數東西」是**假的**。差別在**方向**——少報那兩者會讓規則**更安靜**，而更安靜在那裡代表「鏈還沒完」，不代表「可以收工」。
- **每條規則的轉綠方式**（守衛的失敗訊息若指名了更便宜更壞的修法，它就會被照做的人拆掉，所以先講）。⛔ **這張表不排序**；⚠️ 而且**只有標 ✅ 的兩條實際跑過**，其餘是讀碼推導：

  | 規則 | 轉綠方式 | 要說謊嗎 | 跑過？ |
  |---|---|---|---|
  | `ROUND-CAP` | 把一條鏈拆成同 scope 下兩支帳本 | 否 | 未跑 |
  | `ROUND-CAP` 邊界 | 把 `status=fixed` 改寫成 `open` | **是** | 未跑 |
  | `CHANGE-SUBJECT` | 不記那筆 dead-end | 否 | 未跑 |
  | `UNREVIEWED-FIX` | 不標 `fixed` | 否 | 未跑 |
  | `ROUND-CAP` 預算 | 把一輪的 finding 全部記成 `question` | 否 | 未跑 |
  | `ROUND-CAP` 預算 | **不遞增輪號**（多次審查記在同一個 `round`） | 否 | ✅ 實測 rc=0 |
  | （對照）記帳列 | 只帶 `question` 的列不再花預算 | — | ✅ 實測 5+1 由 rc=1 變 rc=0 |

- ⚠️ **`status` 的 `rejected` 與 `deferred` 是合法值，但沒有任何規則讀它們**（`converge_status.py` 只在 `STATUSES` 驗證集裡出現一次；會分支的只有 `fixed`）。寫它們是給人看的紀錄。⛔ 這條寫出來是因為**已經有人被它騙過**：[#1564](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1564) 的帳本刻意用 `tier=verified, status=rejected` 來區分「沒查」與「查了沒事」，而工具對那個區分完全無感。**不要移除這兩個值**——移除詞彙會讓所有寫過它的既有帳本永久報格式違規。
- **`ROUND-CAP` 的預算只由有審查活動的輪次支出**——該輪至少有一筆 `subject` / `finding` / `dead-end`。只帶 `question` 的記帳列不花錢。在這之前它會花掉一輪（實測：5 個真審查輪 rc=0，同樣 5 輪加一列記帳 rc=1），於是**寫下記帳列的人被罰、不寫的人不被罰**。⛔ 換來的最便宜轉綠寫在上表：把 finding 記成 `question`——那會讓那些 finding 失去 `status`，`UNREVIEWED-FIX` 因此看不到後續的 `fixed`。另外三件同段揭露：⑴ **只記了 `decidability` 而沒宣告 `subject` 的輪次也不花錢**——這是刻意的；⑵ **一輪只做一次審查是慣例，不是工具檢查的事**；⑶ ⛔ **預算不看 `tier`**——任何一筆 `kind=finding` 都讓那一輪算數。**`tier=verified` 決定的是什麼東西跨輪，不是那一輪有沒有發生。**
- 🔴 **比「拆成兩支帳本」更便宜的繞法：不遞增輪號。** 把第 5、6、7、8 次審查全部記在 `round: 5` 底下，`ROUND-CAP` 就數不到（實測：4 個真輪 + 第 5 輪塞 4 次審查 ⇒ rc=0）。⚠️ **這在本次改動之前就存在、行為完全相同**，不是新缺陷；列在這裡是因為**只揭露較貴的那一個，等於暗示較便宜的那個不存在**。
- ⚠️ **`SELF-REVIEW-ZERO` 仍然在數 finding**（它是 advisory 不是停止規則），而且把 `reviewer` 從 `"self"` 改成任何別的字就會消音。**沒有動它**：見 `vibe-subagent-review` references/discipline.md〈預設檔位〉的未解前提。
- `LEDGER-GAP` 只檢查輪號連續，**不檢查是否從 1 開始**。從鏈中途才開帳的 scope 合法且靜默。
- ⚠️ **`KINDS` 是往前看的：拿掉一個 kind，所有寫過它的既有帳本就永久報格式違規、rc=1**，沒有遷移路徑。實例：TRK-360 撤回宣告式 oracle 後，`dev/stopcond` 那本帳本裡 5 筆 `oracle` / `oracle-result` 讓**每一次執行**都 rc=1——讀報告時要先把 `-- FORMAT --` 區塊和停止規則分開看。
- 本工具**不進 CI、不進 pre-commit**、不擋任何東西。這是刻意的：#1457 剛刪掉六支「守衛的守衛」，對 review 流程再造一支 gate 會重演同一個病。owner 分類 = 🧠 **skill-advised**（見 [`hook-vs-skill-coverage.md`](../../../../docs/internal/hook-vs-skill-coverage.md)）。
- 全部規則由**單一一條修正鏈**導出（n=1）。套到別的情境前先自己量。

## 與既有體系關係

- **`vibe-subagent-review`**：管一輪之內怎麼審（lens 路由、finder≠verifier、只報站得住的）。多輪情境下，該 skill 的預設檔位改由本 skill 決定。
- **`vibe-brainstorm`**：還沒開始寫 code 時用它。第 0 步與 brainstorm 的 blast-radius 提問互補——一個問「這題可判嗎」，一個問「炸掉多大」。
- **`PROGRESS.jsonl`**（`vibe-subagent-review` 長時 agent 協議）：那個是**單一 agent 的存活訊號**；`ROUNDS.jsonl` 是**跨輪的知識交接**。格式慣例相同、用途不重疊，同一個 `dev/<scope>/` 下可並存。
