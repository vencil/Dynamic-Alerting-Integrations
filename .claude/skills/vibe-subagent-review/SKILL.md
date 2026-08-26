---
name: vibe-subagent-review
description: IaC-aware 兩階段 review — code 走 spec→quality、IaC 走 blast-radius,含對抗式 review 紀律（finder≠verifier 自審 / verify-before-assert / only-actionable）。Use after a multi-file PR or an `Agent` implementation run, before commit — 特別是改動含 Helm values / .gotmpl / Prometheus rules / VRL transforms（這類「爆炸半徑優先」非單純 code quality）。補 #448 機械 SAST 抓不到的 cross-file cascade（改 selector 連動 NetworkPolicy / ServiceMonitor / ConfigMap 等）。Also use BEFORE spawning long-running（>15 min）reviewer / verifier subagents — 內含長時驗證 agent 可觀測性協議（預設 `Workflow` 編排；raw `Agent` 為例外、須寫 `dev/<scope>/PROGRESS.jsonl` ledger；單 agent ~15 min 上限）。SKIP if change is single-file doc-only or single-file test-only.
---
<!-- 此檔為產生物，來源 agents/skills/vibe-subagent-review/SKILL.md —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->

# vibe-subagent-review — IaC-aware blast-radius review

兩階段 review 的副檔名路由：**code 走 spec→quality，IaC 走 blast-radius**。

機械層的單檔 SAST 由 [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448)（hadolint / kube-linter / trivy config）+ pre-commit 顧；本 skill 顧**機械 lint 抓不到的跨檔語義 cascade**——「改 A 檔讓 B 檔語義錯位」這類需語境推理的問題。owner 分類見 [`hook-vs-skill-coverage.md`](../../../docs/internal/hook-vs-skill-coverage.md)。

## 何時觸發 / 何時跳過

- **觸發**：multi-file PR、或 `Agent` 跑完 implementation 後、`git commit` 前；以及 **spawn 長時（>15 min）reviewer / verifier subagent 前**——預設用 `Workflow` 編排，raw 背景 `Agent` 是須多付 ledger 成本的例外（走下方〈長時驗證 agent 可觀測性協議〉）。
- **跳過**：單檔 doc-only / 單檔 test-only（無 cascade 風險，直接走一般 review）。

## 副檔名路由

| 改動檔 | review lens | 核心問題 |
|---|---|---|
| `.go` / `.py` | **Spec → Quality**（兩階段） | (1) 符合 issue spec？(2) 錯誤處理 / 邊界 / 測試覆蓋？ |
| `values.yaml` / `*.gotmpl` / `Chart.yaml` | **Blast Radius** | selector / RBAC / NetworkPolicy / ConfigMap 連動？ |
| `.vrl` / Vector transform | **Schema cascade** | 下游 SIEM payload field 改了哪些？接收端要通知？ |
| Prometheus rules（recording / alerting） | **Cardinality + Severity** | cardinality 暴增？severity 動到 dedup / Sentinel / 四層路由？ |

## Review 紀律（所有 lens 通用）

上表 domain checklist 決定**查什麼**；這節決定**怎麼報、怎麼驗**——把對抗式 review 紀律 codify 進「觸發時就會讀到」的地方（源自 2026-07 security-audit 方法論萃取）。

**1. 報什麼，以及過濾放哪一端**

⛔ **分界是「內容判準」與「音量判準」，不是「有沒有過濾」。** 兩份官方文件初看相反，實際切在不同軸上：

| 判準的軸 | 放哪一端 | 出處逐字 |
|---|---|---|
| **內容**：只報影響 correctness 或**已宣告需求**的 gap | ✅ **可以**下在 reviewer prompt 裡 | `Tell the reviewer to flag only gaps that affect correctness or the stated requirements, and treat the rest as optional.` |
| **音量／嚴重度**：「只報 high-severity」「保守一點」 | ⛔ **不要**下在 prompt 裡 | `If your review prompt says "only report high-severity issues" or "be conservative," the model may follow that instruction literally and report less; ask it to report everything and filter in a separate pass instead.` |

⚠️ 舊版這裡寫「3 個真問題 > 10 個 style 意見」，那是**音量判準**（叫它少報），已移除。以下三條是**內容判準**，留在 reviewer 端是官方明文支持的：

- 每個 finding = **具體 failure scenario**：什麼 input / state → 什麼壞輸出 / break，附 `file:line`。不是「這樣比較漂亮」「理論上可能」「建議考慮」。
- **designed-behavior**（有 rationale 的刻意設計）不是 bug——先分辨再報。
- **coverage-honesty**：講清楚**沒 review 到**哪些檔 / 路徑；絕不在沒看的地方 imply clean（空 ≠ 安全）。

⚠️ **判別語**：一條指示若讓 reviewer 依「有多嚴重／有多少條」決定閉嘴，放收件端；若讓它依「這是不是一個具體的失效」決定，放 prompt 端。

⛔ **生成物不進 review 視野**（`.claude/**` 是 `agents/**` 的逐位元副本）。實測：一輪外部 review 的 7 條 finding 裡 **3 條落在生成鏡像上**——同一條 finding 被審了兩次。

**2. verify-before-asserting（review finding 是一個 claim）**
- 報 finding 前先 **grep + cite 實際 code** 佐證，不照 pattern-match 的直覺報。（燒過：外部 reviewer 對合法 Workflow-DSL top-level `return` 誤報 illegal-return、對 repo 未 enforce 的 lint 規則亂標——plausible-but-wrong；take / reframe / **reject** 前先驗那條規則 repo CI 真的擋嗎。）
- 收到的「這是 bug」前提（他人 / 外審 / 上一棒）**可能為假** → 親驗，錯了就 reframe。

**3. finder ≠ verifier 自審 pass（方法論核心）**
- 產出 findings 後，**再跑一輪對抗式**：逐條試著**推翻它**——上游有 mitigation 嗎？是 designed-behavior 嗎？data 真的這樣流嗎？測試真的沒蓋嗎？**活不下來的殺掉。**
- 一個 **FIX 可能移除附帶防護** → 重跑原始 invariant 確認沒開新洞。
- **修法 commit 本身是新的受審對象**，不是「原 commit 的附錄」——它常比被審的改動更大，而且沒被任何 lens 掃過。實測（#1431）：第一輪審的是 **814 行新增**，據此產出的修法 commit 是 **1336 行新增**（1.6×）且無人審，第二輪補審它才找到整輪唯一的 **Critical**。「已經審過一輪」永遠是指審過**那一版**。
- **一輪修多條時防 fix-masking**：修法 A 新加的輸出／訊息可能替路徑 B 的缺口作答，讓 B 的回歸案例照樣轉綠、缺口測不出來。逐條驗收要**單獨還原該條修法**看測試轉不轉紅，不要一次還原全部再一起跑。
- 這是 harness 的**單 agent 便宜版**；要**升級到多 agent** 見下節。
- ⚠️ **第 2 輪起，只有「自審 pass 算完整驗證」這一條不算數**——降級為 pre-check，改走〈預設檔位〉的換 context 盲審。本節其餘各條（只報站得住的、verify-before-asserting、FIX 可能移除附帶防護、防 fix-masking）**照常適用，且對盲審一樣適用**。

## 收 review：拿到 finding 之後（借 superpowers `receiving-code-review`）

上面各節管**怎麼發**；這節管**怎麼收**。兩者不對稱：發的時候你在找問題，收的時候你在被說服，而被說服比找問題容易得多。

**1. 每條 finding 先分流，再動手**——`take` / `reframe` / `reject`，逐條給理由：

- **take**：驗過屬實、在本 PR 範圍內 ⇒ 修。
- **reframe**：症狀對、診斷錯 ⇒ 修真正的那個，並說明差在哪。
- **reject**：驗過不成立 ⇒ **附證據**駁回，不是「我覺得還好」。

⛔ **收到的「這是 bug」是 claim 不是事實**，適用上節的 verify-before-asserting。實測（#1481）：CodeRabbit 開 14 條，逐條驗證後 **11 條成立、1 條 reject（附雙模式解析實測，reviewer 自行撤回並記為 learning）、3 條交 owner 判為範圍外**。若照單全收，那 1 條會讓一支能跑的 workflow 被改壞。

**2. 禁止表演性同意。** 不寫「你說得對」「好建議」再開始查。要嘛先驗完再回，要嘛直接動手讓 diff 說話。回覆的價值在**證據與處置**，不在態度。

**3. 一次一條，各自驗收。** 多條一起修會 fix-masking（見上節）；逐條修、逐條跑對應的驗證。

**4. 範圍紀律：finding 指向既有內容時，不要在機械性 PR 裡夾帶政策變更。** 搬檔案的 PR 中 diff 顯示為「新增」是搬移的假象。這類 finding 的正解是**開自己的票、自己的受審主體**，不是搭便車通過。⚠️ 但要**明說缺口是實的**，否則「範圍外」會退化成「不修的藉口」。

**5. ⛔ 每一條 finding 都要走到 thread resolved——`is_outdated` 不等於 `is_resolved`。**

實測（#1481，本 skill 作者親身踩到）：11 條修完並推上去後，GitHub 顯示那些 thread 為 `is_outdated: true`（因為 code 變了），我據此宣稱「14 條全部 resolved」——**而 API 上 `is_resolved` 全是 `false`**。分支保護的「Require conversation resolution」因此持續擋著 merge，我卻把原因誤判成「缺 approving review」。逐條 resolve 後 PR 立刻可 merge。

⇒ **三種處置各有自己的收尾，缺一不可**——`reject` 沒有 code fix，所以最容易被漏掉，而未 resolve 的 rejected thread 擋 merge 的力道與其他兩種**完全一樣**：

| 處置 | 收尾 |
|---|---|
| `take` | 修 → 回覆處置 → **resolve** |
| `reframe` | 依重新框定後的問題修 → 回覆說明改了什麼、為何不是原本那條 → **resolve** |
| `reject` | **無 code fix** → 回覆**附證據**的駁回理由 → **resolve** |

⇒ 只 resolve 你**真的處置過**的：三種處置都必須先有那一則回覆。沒修、也沒寫理由就順手清掉，等於用 resolve 把 finding 埋掉。

## 鷹架准入：兩道門，缺一不可

修 finding 最常見的產物是**一支新測試或一支新守衛**，而那正是本 repo 缺陷密度最高的地方。動手寫之前過兩道門：

**門 1（寫測試 body 之前）** — 借 superpowers `writing-good-tests.md` 逐字：
> `BEFORE writing the test body: Name the production change that would make this test fail. Cannot name one → redesign around an observable behavior`

同一份檔案的 warning signs 裡，這三條**逐字命中本 repo 的既有形狀**，看到就停：
> `The test greps source text, or asserts a removed symbol stays removed`
> `The test exists for coverage, checking no side effect or outcome`
> `Setup and assertion share the same object, guaranteeing equality`

⚠️ 原文寫 `production change`。這條 repo 有大量文件／流程／dx 鏈，對它們照字面讀會變成寫不出來 ⇒ 讀成「**哪一個改動**（程式、產物、設定皆可）」。⛔ 但不可退化成「改掉被測的東西」——那恆真。

**門 2（守衛值不值得留）**：
> **守衛值得留，當且僅當它的靜默失效會讓「它要防的那個缺陷」回來。**
不會 ⇒ 那是**揭露級**不是守衛級：原地寫「⚠️ NOT GUARDED：<實測到的存活事實>」＋量測，不要寫「已涵蓋」。
⚠️ 寫「它要防的那個缺陷」而不是「原始缺陷」：**預防性守衛**（為還沒燒過的類別而寫）沒有「原始」缺陷可以回來，照字面讀會被這道門擋掉。

⚠️ **刪掉一條規則時，把只斷言它「不出現」的測試一起刪掉。** 那種測試在規則消失後會變成**恆真**而全綠——實測發生過：刪一條規則時 5 支相關測試只有 2 支轉紅，另外 3 支因為斷言的是「該字串不出現」而靜默變成永遠通過。

⚠️ **新守衛要對自己跑變異，而且兩個方向都要。** 實測：把一個上限常數從 5 改成 **500** 時測試全綠——因為 fixture 是**從那個常數建的**，常數一改 fixture 跟著改。改成字面量 + 一支釘住常數的 pin 之後，500 與 4 兩個方向都轉紅。**「下限不可取自它要保護的東西」在測試 fixture 上同樣成立。**

## 預設檔位（第 1 輪 vs 第 2 輪起）

同一個缺陷的**第幾輪**修正，決定上節自審夠不夠：

| 情境 | 預設 | 理由 |
|---|---|---|
| **第 1 輪**（新實作 / 例行 multi-file PR） | 上節 finder≠verifier **自審**足夠 | 便宜、涵蓋大多數情況；不為例行 review gold-plate |
| **第 2 輪起**（修法、re-fix、對同一 issue 再改） | ⛔ 自審**降為 pre-check**，預設改為**換 context 的盲審** | 實測：#1457 前三顆 commit「合計 8 位、59+ 條 finding、61 個單點變異，**作者自審 0 條**」。同一個 context 已經對自己的修法失明——它剛把每條路徑都說服過自己一次 |

「換 context」不必然等於多 agent harness：另開一個**不帶本輪對話**的 reviewer（新 session / 新 subagent / 另一個模型皆可），只餵**受審 diff 本身**與 [`vibe-converge`](../vibe-converge/SKILL.md) 的跨輪交接三件組（verified claim / open question / 已打死方向表）。⛔ **不要餵上一輪的完整 commit body 與修法敘事**——那正是讓下一棒繼承上一棒盲區的東西。

📋 **可照抄的 prompt 骨架**：[`references/scoped-re-review.md`](references/scoped-re-review.md)（借 superpowers `re-review-prompt.md`，與原版的三處分歧已在該檔標出）。⛔ **骨架裡承重的那一格在收件端不在 prompt 端**：reviewer 照報所有東西（音量軸不下 prompt），但落在修法 diff 之外的寫進〈範圍外觀察〉，**不擋本輪、不開下一輪**，收下時記成 `kind=question`——`converge_status.py` 對 `question` 只計數不判定，所以不會經由 `UNREVIEWED-FIX` 把迴圈拉長。⚠️ 這一格在本 repo **從未存在過**（本協議自己那條鏈的 20 條 finding 每一條都餵進了下一輪），效果**未量**：第一次用完把「本輪幾條 / 其中幾條落進範圍外」記進帳本。

多輪情境的停止時機、什麼時候該**換受審主體**而不是再修一版，走 [`vibe-converge`](../vibe-converge/SKILL.md)（`make converge-status`）。

⛔ **輪數上限 5，而且停止條件不是「審到零 finding」**——那條規則已刪除，理由（單次 inspection 中位只撈到 30%、61% 的 review 找到零缺陷、Fagan/Cisco/NASA 的 exit criteria 是「已知缺陷已修且已驗證」）見 vibe-converge 與其 `references/derivation.md` §4.1。⚠️ 上限是**預算不是判準**：它說「你用完了」，不說「你做完了」。

> ⚠️ **本節整體有一個未解的前提，寫在這裡而不是靜靜照舊（2026-08-26）。** 當前模型的官方 prompting 指引逐字說：`Claude Opus 5 verifies its own work without being told to. If your prompt contains explicit verification instructions … remove them: instructions like these cause over-verification`，並且 `The same applies to legacy harness scaffolding that adds separate verification steps`；其建議的 delegation 條款含 `do not use subagents to verify or double-check your own work`。
> 本節就是那種鷹架。與它相反的本 repo 實證（#1457「8 位盲審 59+ 條，作者自審 0 條」）是在**更早的模型世代**上量的。
> ⇒ **兩邊都沒有在當前模型上被量過，所以本次不動這一節。** 關掉它的方式是側量，不是選邊：同一支 PR、同一顆 SHA，自審一次並記下 finding 集合，再派**一位**盲審審同一顆 SHA，比兩個集合的差集。在那之前，把本節讀成「尚未驗證的既有做法」，不是「已證實的必要步驟」。

## 升級到多 agent harness（大 / 高風險 review；defer-with-trigger）

上節 finder≠verifier **自審**是單 agent 便宜版。改動**很多檔 / 高 blast-radius IaC / 跨 component**、或 stakes 值得時，升級到多 agent——直接 reuse [`vibe-security-audit`](../vibe-security-audit/SKILL.md) 的 Workflow pattern，只換 lens：

- **dimensions**（correctness / IaC-blast-radius / reuse-simplify）→ 各一個 **finder** subagent → 每個 finding 由**不同模型** validator「DISPROVE」→ synthesize survivors、ranked、only-actionable。
- **模型分層**：強模型找、便宜模型驗（見 security-audit 的 `audit-workflow.js`）。編排走下方〈長時驗證 agent 可觀測性協議〉的 Workflow-first。

⚠️ **不要對例行 multi-file PR 起這個**——它跟 security-audit 一樣貴，是**刻意的升級 tier**、非預設；例行 review 走上節單 agent 自審即可（MVP、不 gold-plate 例行 review）。真需要時再建 review-Workflow（現在 reuse security-audit harness，不另造）。

## Spec → Quality（`.go` / `.py`）

兩階段，分開跑（避免「code 漂亮但沒做對事」）：

1. **Spec 符合度**：對照 issue / ticket，做的是不是「要做的事」？範圍有無 over/under？
2. **Code quality**：錯誤處理、邊界、並發、測試 seam（用對 `freshMetrics` / FakeClock，見 [`test-map.md`](../../../docs/internal/test-map.md)）、tenant-agnostic（dev-rule #2）。

> **Go `Close()` 讀/寫不對稱**（review 必查，errcheck 分不出）：`defer func(){ _ = x.Close() }()` 只對 **read-closer** 安全（`resp.Body` / `sql.Rows` / `os.Open` 唯讀檔——關閉只釋放資源）。**write-closer**（`os.Create` / `gzip.Writer` / 自訂 `io.WriteCloser`）的 `Close()` error **不可吞**——寫入的 disk-flush 常延到 `Close()` 才發生，吞掉 = silent data loss。
>
> 盲區：自訂介面（如 `GetStorage() TenantStateStorage`，內嵌 `io.WriteCloser`）AI/review 缺全域 context 判不出讀/寫，易把 `_ = store.Close()` 誤當資源釋放放行。**正規防禦 = named return + defer 捕捉**（一眼可辨、且不必逐處判斷讀/寫）：⚠️ 精確範圍——`defer` 在 **early return** 時把 `Close()` 的 error 併進具名回傳值；**panic 展開時它一樣會執行，所以 `Close()` 必定被呼叫**，但它**不會 `recover()`、也不會把 panic 轉成 error**，panic 照常向上傳。要攔 panic 得另外明文寫 `recover()` 政策。
>
> ```go
> func WriteTenant() (err error) {
>     f, err := os.Create(p)
>     if err != nil { return err }
>     defer func() { err = errors.Join(err, f.Close()) }() // disk-flush 錯誤必上傳
>     // ... 寫入 ...
>     return nil
> }
> ```
>
> （來源：#912 + #914 對抗 review）

## Blast-radius checklist（`values.yaml` / template）

改 Helm values / template 時逐項問：

- [ ] **label / selector 改了** → 哪些 Service / ServiceMonitor / NetworkPolicy / Prometheus relabel 跟著要改？（漏改 = metric 靜默斷採集）
- [ ] **resource / replica / PVC 改了** → 容量 / scheduling / PDB / HPA 影響？
- [ ] **securityContext / capabilities.add 改了** → 有 rationale 註解嗎？（#448 Mode B 要求）
- [ ] **新增 ConfigMap / Secret key** → consumer 端 mount / envFrom 對齊？
- [ ] **subchart enabled flag** → RBAC / CRD / namespace / 依賴 chart 連動？

## Schema-cascade checklist（`.vrl` / transform）

- [ ] **改 / 刪 field** → 下游消費者（SIEM / dashboard / alert rule）哪些依賴它？
- [ ] **rename** → 有無相容過渡（雙寫 / alias），或需同步改下游？
- [ ] **型別變更**（string→int、scalar→array 等）→ 下游 parser / schema 會不會炸？

## Cardinality + Severity checklist（Prometheus rules）

- [ ] **新增 label / 動 label 來源** → cardinality 估算，需不需 Cardinality Guard opt-in（dev-rule #8）？
- [ ] **改 severity** → 動到 Severity Dedup / Sentinel / 四層路由 哪一層？（見 architecture-and-design 設計概念）
- [ ] **改 recording rule 名** → 下游 alerting rule / dashboard 引用是否同步？

## Worked examples（範式；具體案例隨真實觸發累積）

> 初版列**結構範式**而非 fabricated PR 引用（避免假造）；真實 worked example 會隨 skill 觸發逐步補入。

- **`.go`**：tenant-api 新增 handler → Spec：是否含 tenant-scoped authz？Quality：async 路徑是否用 `pollUntilTerminal` 取代 blind sleep（TRK-224 pattern）？
- **Helm values**：改 `victorialogs` pod label → Blast：對應 ServiceMonitor 的 selector 還命中嗎？Prometheus relabel 規則？
- **VRL**：chargeback transform 改輸出 field → Schema cascade：chargeback CSV schema + 下游 finance pipeline 的 `sha256sum -c` 驗證是否受影響。

## 長時驗證 agent 可觀測性協議（預估 >15 分鐘必守）

> **動機**（2026-07-04 security-audit fix-重驗實測）：兩個對抗式 verifier 以單一背景 `Agent` 各跑 46–71 分鐘，完成前零訊號——`.output` transcript 不能 tail（全量 JSONL 會撐爆 parent context），唯一觀測手段是反覆翻 `dev/<scope>/` 下的隨機 scratch 檔；其中一個卡在 PromQL 括號平衡的過度優化上燒掉 ~71 分鐘，中途無從察覺、無從止損。

### 三條規則

1. **Workflow-first**：多階段 verify / review（≥2 個里程碑、或預估 >15 分鐘）一律用 `Workflow` 工具編排，**不用單一長時背景 `Agent`**——`phase()` / `log()` 原生串流到 `/workflows` live view，且天然把工作拆成多個短 staged agent（單 agent 負擔低、可觀測性內建、可 resume）。`vibe-security-audit` 的 `audit-workflow.js` 即此 pattern。
2. **raw `Agent` 例外 → 強制 progress ledger**：確有理由用單一背景 `Agent`（單一不可分割里程碑）時，spawn prompt **必須**內嵌下方 ledger 契約（`<scope>` 代換為實際 scratch 目錄，如 `dev/sec741/verify1`）——agent 每過一個里程碑就 append 一行到 `dev/<scope>/PROGRESS.jsonl`。parent 之後 cheap-poll 這個小檔即可（`make agent-progress`），不撈 transcript、不猜 scratch 檔。parent 看到 `blocked` 或連續 `fail` 時**主動介入**（停掉、帶著 ledger 尾端 reframe 後重 spawn），不陪它燒完。
3. **單 agent 範圍上限 ~15 分鐘**：預估超過就拆成 staged agents——每段收在一個 checkpoint、前段結論以文字餵給下段（`PROGRESS.jsonl` 就是天然的交接摘要：下一棒讀 ledger，不讀前棒充滿錯誤嘗試的 transcript）。一個 agent 卡死應在 15 分鐘內被看見，而不是 71 分鐘後才知道。

### Ledger 契約（原樣貼進 spawn prompt）

```text
進度回報（強制）：每完成一個里程碑，append 一行 JSON 到 dev/<scope>/PROGRESS.jsonl
（echo '{...}' >> dev/<scope>/PROGRESS.jsonl；append-only——不重寫、不刪行、不換檔名）：
  {"ts":"<UTC ISO-8601，取自 date -u +%FT%TZ>","stage":"<里程碑>","status":"ok|fail|blocked","note":"<一句話>"}
note 禁含單/雙引號、反斜線、換行（要引用改全形「」）；有 jq 的環境（如 dev container）
優先 jq -nc --arg 建行（自動逃逸）；host Git Bash 無 jq，用上行 echo 模板即可。
驗證類工作的 stage 順序：gate-mapped → repro-built → repro-ran → verdict。
同一 stage 連續失敗/重試 ≥3 次仍未過 → 必須寫一行 status=blocked 註明卡點，並換路徑或
給部分結論收尾——嚴禁盲目重試（觸發條件用次數不用時間：LLM 沒有內部時鐘，數得準的是次數）。
會跑外部指令（測試/編譯/查詢）且有掛起風險者，一律 timeout 5m <command> 包裹；逾時記一行 fail。
```

### 觀測與反模式

- 觀測：`make agent-progress SCOPE=dev/<scope>`（`N=10` 調 tail 行數）——列出 SCOPE 下所有 `PROGRESS.jsonl` 尾端，並對 >15 分鐘未更新的 ledger 印 LIVENESS 警告（agent 自報進度之外的外部存活探針，抓 zombie／掛死）。
- ⛔ tail agent 的 `.output` transcript（全量 JSONL 撐爆 parent context——這正是 ledger 存在的理由）。
- ⛔ 把 scratch 檔當進度訊號（非結構化、路徑靠猜、要反覆全掃）。
- ⛔ 長時 agent 只在完成時 flush 結果（中途不可觀測 = 不可止損）。

## 與既有體系關係

- **[#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448)**（container/k8s SAST lint）：機械層單檔 violation（runAsNonRoot / hostNetwork / ALLOW_EMPTY_PASSWORD…）。本 skill 是 AI 跨檔語義層——**互補不重做機械 lint**。
- **vibe-dev-rules**：commit / branch / trailer 紀律仍以 dev-rules 為準（本 skill 不重做）。
- **[vibe-converge](../vibe-converge/SKILL.md)**：本 skill 管**一輪之內**怎麼審；輪與輪之間傳什麼、何時停、何時換受審主體由它管。第 2 輪起兩者一起用。
- **vibe-security-audit**：稽核 harness 本體已是 Workflow 編排（原生串流）；稽核後 fix 的對抗式重驗 verifier 屬本 skill 長時驗證協議的適用對象。
- 優先級仲裁見 [CLAUDE.md §Skill 優先級宣告](../../../CLAUDE.md)；衝突時 `vibe-*` supersede 環境層 `engineering:code-review`。
