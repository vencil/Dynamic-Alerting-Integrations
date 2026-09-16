---
name: vibe-subagent-review
description: IaC-aware 兩階段 review — code 走 spec→quality、IaC 走 blast-radius,含對抗式 review 紀律（finder≠verifier 自審 / verify-before-assert / only-actionable）。Use after a multi-file PR or an `Agent` implementation run, before commit — 特別是改動含 Helm values / .gotmpl / Prometheus rules / VRL transforms（這類「爆炸半徑優先」非單純 code quality）。補 #448 機械 SAST 抓不到的 cross-file cascade（改 selector 連動 NetworkPolicy / ServiceMonitor / ConfigMap 等）。Also use BEFORE spawning long-running（>15 min）reviewer / verifier subagents — 內含長時驗證 agent 可觀測性協議（預設 `Workflow` 編排；raw `Agent` 為例外、須寫 `dev/<scope>/PROGRESS.jsonl` ledger；單 agent ~15 min 上限）。SKIP if change is single-file doc-only or single-file test-only.
---
<!-- 此檔為產生物，來源 .agents/skills/vibe-subagent-review/SKILL.md —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->

# vibe-subagent-review

機械層的單檔 SAST 由 [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) 的 hadolint／kube-linter／trivy 與 pre-commit 顧；本 skill 顧**跨檔語義 cascade**（owner 分類見 [`hook-vs-skill-coverage.md`](../../../docs/internal/hook-vs-skill-coverage.md)）。輪與輪之間怎麼傳、何時停，由 [`vibe-converge`](../vibe-converge/SKILL.md) 管；commit／branch／trailer 紀律以 [`dev-rules.md`](../../../docs/internal/dev-rules.md) 與 CLAUDE.md／AGENTS.md 不可協商項為準，本 skill 不重做；與環境層 skill 衝突時依 [CLAUDE.md §Skill 優先級宣告](../../../CLAUDE.md)，`vibe-*` supersede `engineering:code-review` 的 git/commit/branch/trailer 部分。

⛔ `.claude/skills/**` 與 `.claude/agents/**` 是 `.agents/**` 的生成鏡像，不進 review 範圍（`.claude/hooks/` 與 `settings.json` 是手寫來源，照審）（實測一輪 7 條 finding 有 3 條打在鏡像上、同一條審兩次）。

## 副檔名路由

| 改動檔 | lens | 核心問題 |
|---|---|---|
| `.go` / `.py` | Spec → Quality（兩階段分開跑） | (1) 做的是不是 issue 要的事、範圍有無 over/under？(2) 錯誤處理／邊界／並發／測試 seam（[`test-map.md`](../../../docs/internal/test-map.md)）／tenant-agnostic |
| `values.yaml` / `*.gotmpl` / `Chart.yaml` | Blast radius | 下表 |
| `.vrl` / Vector transform | Schema cascade | 下表 |
| Prometheus rules | Cardinality + Severity | 下表 |

**Blast-radius checklist（Helm）**
- label / selector 改了 → Service / ServiceMonitor / NetworkPolicy / Prometheus relabel 跟著改了嗎？漏改＝metric 靜默斷採集
- resource / replica / PVC 改了 → 容量 / scheduling / PDB / HPA
- securityContext / capabilities.add 改了 → 有 rationale 註解嗎（#448 Mode B）
- 新增 ConfigMap / Secret key → consumer 端 mount / envFrom 對齊了嗎
- subchart enabled flag → RBAC / CRD / namespace / 依賴 chart

**Schema-cascade checklist（VRL）**
- 改／刪 field → 下游 SIEM / dashboard / alert rule 誰依賴它
- rename → 有相容過渡（雙寫／alias）嗎
- 型別變更 → 下游 parser / schema 會不會炸

**Cardinality + Severity checklist（Prometheus rules）**
- 新增 label / 動 label 來源 → cardinality 估算，要不要 Cardinality Guard opt-in（dev-rule #8）
- 改 severity → 動到 Severity Dedup / Sentinel / 四層路由哪一層（[architecture-and-design](../../../docs/architecture-and-design.md)）
- 改 recording rule 名 → 下游 alerting rule / dashboard 引用同步了嗎

## 收 review：三種處置各有收尾，缺一則 merge 被擋

| 處置 | 收尾 |
|---|---|
| take（驗過屬實） | 修 → 回覆處置 → resolve thread |
| reframe（症狀對、診斷錯） | 修真正那個 → 回覆說明差在哪 → resolve |
| reject（驗過不成立） | 回覆**附證據** → resolve |

新增或修改的回歸測試要做一次 intentional-break：還原修法 → 跑測試轉紅 → 恢復 → 轉綠（[testing-playbook §v2.8.0 LL 第 6 條 Intentional-break dogfood](../../../docs/internal/testing-playbook.md)）；沒做過這一輪的測試是 article-of-faith。

⛔ **GitHub 的 `is_outdated` 不等於 `is_resolved`；分支保護只看後者，reject 沒有 code fix 所以最容易漏 resolve。** 一次一條、各自驗收，不要多條一起修（fix-masking）。收到的「這是 bug」是 claim，先驗再回，不寫表演性同意。

## 長時 reviewer / verifier（預估 >15 分鐘）

多階段（≥2 個里程碑、或預估 >15 分鐘）一律用 `Workflow` 編排；不得已用單一背景 `Agent`（單一不可分割里程碑）時，spawn prompt 必貼 [`references/discipline.md`](references/discipline.md) 的 ledger 契約，之後用 `make agent-progress SCOPE=dev/<scope>` 看進度，不要 tail transcript；看到 `blocked` 或連續 `fail` 就主動介入（停掉、帶著 ledger 尾端 reframe 後重 spawn），不陪它燒完。單 agent 上限約 15 分鐘，超過就拆段。

⛔ **同一缺陷第 2 輪起（修法、re-fix），自審降為 pre-check，預設改派不帶本輪對話的盲審**（[`references/discipline.md`](references/discipline.md)〈預設檔位〉；prompt 骨架在 [`references/scoped-re-review.md`](references/scoped-re-review.md)）。

## 紀律（不確定怎麼審、怎麼派、怎麼判鷹架時讀）

[`references/discipline.md`](references/discipline.md)：Review 紀律（報什麼、過濾放哪一端、verify-before-asserting、finder≠verifier 自審）、收 review 全文、鷹架准入兩道門、第 1 輪與第 2 輪起的預設檔位、升級多 agent 的條件、Spec → Quality 與 Go `Close()` 讀寫不對稱、長時 agent 協議全文。第 2 輪起派盲審的 prompt 骨架在 [`references/scoped-re-review.md`](references/scoped-re-review.md)。
