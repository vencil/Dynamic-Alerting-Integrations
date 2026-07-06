---
name: vibe-subagent-review
description: IaC-aware 兩階段 review — code 走 spec→quality、IaC 走 blast-radius,含對抗式 review 紀律（finder≠verifier 自審 / verify-before-assert / only-actionable）。Use after a multi-file PR or an `Agent` implementation run, before commit — 特別是改動含 Helm values / .gotmpl / Prometheus rules / VRL transforms（這類「爆炸半徑優先」非單純 code quality）。補 #448 機械 SAST 抓不到的 cross-file cascade（改 selector 連動 NetworkPolicy / ServiceMonitor / ConfigMap 等）。Also use BEFORE spawning long-running（>15 min）reviewer / verifier subagents — 內含長時驗證 agent 可觀測性協議（預設 `Workflow` 編排；raw `Agent` 為例外、須寫 `dev/<scope>/PROGRESS.jsonl` ledger；單 agent ~15 min 上限）。SKIP if change is single-file doc-only or single-file test-only.
---

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

**1. 只報站得住的（concrete > theoretical）**
- 每個 finding = **具體 failure scenario**：什麼 input / state → 什麼壞輸出 / break，附 `file:line`。不是「這樣比較漂亮」「理論上可能」「建議考慮」。
- **3 個真問題 > 10 個 style 意見**；別用 nit 灌厚度。**designed-behavior**（有 rationale 的刻意設計）不是 bug——先分辨再報。
- **coverage-honesty**：講清楚**沒 review 到**哪些檔 / 路徑；絕不在沒看的地方 imply clean（空 ≠ 安全）。

**2. verify-before-asserting（review finding 是一個 claim）**
- 報 finding 前先 **grep + cite 實際 code** 佐證，不照 pattern-match 的直覺報。（燒過：外部 reviewer 對合法 Workflow-DSL top-level `return` 誤報 illegal-return、對 repo 未 enforce 的 lint 規則亂標——plausible-but-wrong；take / reframe / **reject** 前先驗那條規則 repo CI 真的擋嗎。）
- 收到的「這是 bug」前提（他人 / 外審 / 上一棒）**可能為假** → 親驗，錯了就 reframe。

**3. finder ≠ verifier 自審 pass（方法論核心）**
- 產出 findings 後，**再跑一輪對抗式**：逐條試著**推翻它**——上游有 mitigation 嗎？是 designed-behavior 嗎？data 真的這樣流嗎？測試真的沒蓋嗎？**活不下來的殺掉。**
- 一個 **FIX 可能移除附帶防護** → 重跑原始 invariant 確認沒開新洞。
- 這是 harness 的**單 agent 便宜版**；要**升級到多 agent** 見下節。

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
> 盲區：自訂介面（如 `GetStorage() TenantStateStorage`，內嵌 `io.WriteCloser`）AI/review 缺全域 context 判不出讀/寫，易把 `_ = store.Close()` 誤當資源釋放放行。**正規防禦 = named return + defer 捕捉**（一眼可辨、且擋 panic / early-return 漏判，不靠判斷讀/寫）：
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
- **vibe-security-audit**：稽核 harness 本體已是 Workflow 編排（原生串流）；稽核後 fix 的對抗式重驗 verifier 屬本 skill 長時驗證協議的適用對象。
- 優先級仲裁見 [CLAUDE.md §Skill 優先級宣告](../../../CLAUDE.md)；衝突時 `vibe-*` supersede 環境層 `engineering:code-review`。
