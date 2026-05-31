---
title: "DEC-F (#457) R0 評估書 — Rule Pack × threshold-calculator 資料流"
short: Rule Pack × threshold-calculator 資料流 R0 三方 review 評估
audience: [maintainers]
tags: [internal, planning, rfc, threshold-calculator, rule-pack]
status: accepted
lang: zh
version: v2.8.1
---

# DEC-F (#457) R0 評估書 — Rule Pack × threshold-calculator 資料流

> ## ✅ R0 DECISION: DEFINITIVE (2026-05-31)
>
> 三方 review 完成（Claude 評估書 + Gemini 外審整合 + maintainer 採納），DEC-F 從「未定」→ **definitive**：
>
> - **STAGE-1 採納** — `threshold_recommend --export-patch`（可 `git apply` 的 conf.d unified diff，T1.5 ~100 LOC / 0 新依賴）。**另開實作票**。
> - **DEFER（重 adapter）** — calculator → da-batchpr 自動開 PR，trigger = §4-Q4 的 T1/T2/T3。
> - **REJECT** — 寫回 rule pack schema / `calculator:` 子段（破壞 declarative 純度 + 衝擊客戶 GitOps repo）。
> - **前置票（最優先）** — 先驗/修 `threshold_recommend` query series（§6），為 STAGE-1 實作的前置。
> - **Future Work（獨立票）** — drift lint / global default drift 報表 / portal inline 建議。
>
> 落地：CHANGELOG `[Unreleased]` + `roadmap-future.md` 已更新；follow-up tickets 見 §11。本文以下為決議的完整論證依據。

> **用途**：本文是 [#457](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/457) R0 三方 review（maintainer + 外部 adversarial review + Claude）的 **Claude 側輸入**。
> 提供結構化分析 + 帶 trade-off 的建議；上方決議框為三方拍板結果。
> 產出方法：`vibe-brainstorm` 五問發散 → 收斂草案 → 一輪外審（Gemini）take/reframe/reject → 兩輪 ground-in-repo 探索。

---

## 0. TL;DR（建議，待拍板）

| | 內容 | 理由一句話 |
|---|---|---|
| **STAGE-1（小 do，論據強）** | `threshold_recommend --export-patch` 出**可 `git apply` 的 conf.d unified diff** | ~100 LOC、0 新依賴、自動繼承既有 backtest CI 風險報告；除掉手動 copy toil 又保留 human review |
| **DEFER（重 adapter）** | calculator → da-batchpr **自動開 PR** | 省的只有 operator 的 `git commit`、帶 auto-merge blast-radius、無客戶拉力；給具體 trigger |
| **REJECT（不變）** | 寫回 rule pack schema / 加 `calculator:` 子段 | 破壞 declarative 純度 + 衝擊客戶 GitOps repo；conf.d 才是閾值領域 |
| **前置票（最優先）** | 先驗/修 `threshold_recommend` 的 query series | 「建議對不對」是「要不要自動化建議」的前提，比任何 wiring 都先 |
| **Future Work（獨立小票）** | (a) rule-pack 註解↔`_defaults.yaml` drift lint；(b) global default drift 報表；(c) portal inline 建議 | 各有自己 trigger，不混進 STAGE-1 |

---

## 1. 現況查核（verify-don't-claim，2026-05-31 對 main）

| 查核點 | 實況 | 證據 |
|---|---|---|
| threshold-calculator 工具 | `baseline_discovery.py` / `threshold_recommend.py` / `backtest_threshold.py` 三支獨立 CLI，皆有測試 | `scripts/tools/ops/*.py` + `tests/ops/test_*.py` |
| calculator → conf.d 寫入接線 | **無**。`threshold_recommend --json` 註明 "for pipeline integration"，但無任何 consumer | `threshold_recommend.py:18-19`；`batchpr_dispatch.py`（grep calculator = 0 hits） |
| 現行流向 | calculator 出 JSON/CSV/Markdown「給人看的建議」→ **人工** copy 到 `conf.d/<tenant>.yaml` | issue §3；無自動 writer |
| **backtest 已進 CI** ⭐ | `backtest.yaml`：改 `conf.d/**` 的 PR 自動跑 `backtest --git-diff --markdown-output` → sticky comment 貼 old-vs-new 觸發次數風險報告（`continue-on-error`） | `.github/workflows/backtest.yaml:32-45` |
| Rule Pack schema | 純 `groups → rules`；PromQL 用 `user_threshold{...}` recording rule **讀取**閾值，**不 hardcode 數值**；rule pack 僅有 doc-comment 鏡像 default 值 | `rule-packs/rule-pack-mariadb.yaml:9-11, 56-70` |
| conf.d 寫入者（共 4） | (a) da-batchpr apply/refresh；(b) **tenant-api `gitops/writer.go`**（live Save→commit / PR）；(c) profile build（onboarding）；(d) 單筆 override CLI = **不存在** | `batchpr_dispatch.py`；`components/tenant-api/internal/gitops/writer.go:163-178, 451-609`；ADR-018 |
| profile build vs recommend | profile build = onboarding **一次性**（corpus 導入有完整 5-step 旅程）；recommend = ongoing tuning **advisory**（無對應旅程）→ 留成手動是架構一致的選擇 | ADR-018 scope；`roadmap-future.md:30` |
| rule-pack 註解 ↔ `_defaults.yaml` drift lint | **不存在**；註解可默默 stale（tech debt） | `.pre-commit-config.yaml`（無相關 check） |
| portal | `threshold-calculator.jsx` 為 standalone、hardcoded profiles、**MOCK，未接 live recommend** | `tools/portal/src/interactive/tools/threshold-calculator.jsx` |
| #423 version-aware | 今日 DONE；deferred 清單**不含**本議題 | epic closure note |
| roadmap 定位 | v2.9.0「維運自動化 — Rule Pack × threshold-calculator 資料流**閉環**評估」 | `roadmap-future.md:92` |
| 客戶拉力訊號 | **無**。無 issue/RFP 顯示客戶因人工 copy toil 受阻 | 全 repo 無 customer signal |

**結論：issue 前提全數成立**——資料流仍各自獨立、決策仍 pending、未被 #423 吸收。**仍需執行**（執行=拍板，非寫 feature code）。

---

## 2. 一個必須先拆掉的錯誤前提（reframe，維持）

issue §3 寫「→ 寫回 **Rule Pack** 或 `_defaults.yaml`」。但架構已分層：

- **Rule Pack** = domain expert 的 declarative PromQL（recording/alert + doc-comment 參考值），canonical、近 read-only。PromQL 本身用 `user_threshold{...}` **讀**閾值，不持有 operative 數值。
- **conf.d（`_defaults.yaml` + `<id>.yaml`）** = per-tenant 閾值領域，operative 數值住這、**已有 writer**（profile build / tenant-api）。

calculator 算的是 per-tenant 數值 → 天然屬 conf.d。寫回 rule pack 會破壞 declarative 純度（issue "Against" #2）+ 觸發 schema 改動衝擊客戶 GitOps repo（issue "Against" #3）。**故「`calculator:` 子段 / rule pack schema 改動」在 R0 直接 REJECT。** 真問題收斂為：

> 要不要把 `threshold_recommend` 的建議值**自動帶進 conf.d edit**，去掉人工 copy？

---

## 3. 關鍵發現：所謂「閉環」其實已建好 ~90%

把現有零件攤開，「calculator → conf.d 閉環」的兩半已存在：

| 環節 | 狀態 | 工具 |
|---|---|---|
| **建議半** — current/recommended/delta/confidence + markdown | ✅ 已建 | `threshold_recommend`（`KeyRecommendation` 已含 `recommended`、`delta_pct`；`threshold_recommend.py:124-149, 311-322`） |
| **審查證據半** — old vs new 觸發次數風險報告 PR comment | ✅ 已建且**已進 CI** | `backtest.yaml:32-45` |
| **唯一缺口** — 把 `recommended` 帶進 conf.d edit | ❌ 人工 copy | 無 |

→ 補上缺口後整鏈閉合：
`threshold_recommend --export-patch → operator git apply + commit + 開 PR → 既有 backtest.yaml 自動貼風險 comment → 人 review → merge`
**唯一新增是把建議格式化成 patch；其餘四個零件全是重用。**

---

## 4. vibe-brainstorm 五問

### Q1 Reuse-over-build
receiving infra **全部已存在**（da-batchpr / tenant-api writer / da-guard / backtest-in-CI）。STAGE-1 不是造輪子，是把既有建議格式化成可套用 patch。反面：reuse 紀律也說「別 speculative 造沒 consumer 的輪子」——目前**無 consumer 在喊**，故重的 auto-PR 部分該緩。

### Q2 MVP vs Future Work（見 §5 成本分級）
- **STAGE-1**：`--export-patch` 出可 apply 的 conf.d diff（T1.5）。
- **明確 drop**：rule pack `calculator:` 子段（§2 reject）、auto-PR（§5 DEFER）、continuous 自動重算、anomaly-aware adaptive（roadmap「探索方向」另列）、portal inline 建議（greenfield + demo 陷阱）。

### Q3 explicit trade-off — audit-trail 價值的修正
- **外審（Gemini）提的 audit-trail 價值大半已存在**：backtest CI 已在任何 conf.d PR 自動貼 old-vs-new 風險報告（`backtest.yaml`）。故 audit-trail 不是「auto-PR」的新增 upside，而是 **STAGE-1 免費繼承**的既有資產。
- **價值 ∝（tenant 數 × 重算頻率）**；無客戶大規模 + 無高頻重算 → 重 adapter 的 now-value 低，卻新增維護 + blast-radius。

### Q4 Defer-with-trigger（T3 機制 reframe）
- 外審準確指出 passive trigger 雞生蛋陷阱：工具難用→客戶默默棄用→收不到 T1 抱怨→defer 偷偷變 wontfix。
- 但外審的 T3「用 telemetry 觀測 recommend 執行次數」**機制不可行**（客戶在自己環境跑 CLI，平台收不到）。
- **reframe**：消滅雞生蛋最便宜的辦法就是**現在 ship STAGE-1**（工具不再難用，就不必等不會來的抱怨）。重 adapter 的 trigger 改為我們真能控的：
  - **T1**：客戶在 ≥N tenant 跑 recommend 後回報 copy/commit toil；
  - **T2**：客戶 RFP 要 continuous / 自動化 tuning pipeline；
  - **T3（主動 poll）**：maintainer 在 quarterly-audit / 客戶 check-in 主動問「有在跑嗎？patch→PR 流程順不順？」

### Q5 Blast-radius / failure mode — auto-merge 不對稱（take 外審）
- 自動 flow 把壞建議變成自動 PR 的閾值；blast-radius > 人工。
- **外審的不對稱論點正確且採納**：**調高閾值→漏報（False Negative，致命）**；調低→報警疲勞（可控）。故 **review 絕不能省，尤其放寬告警時**，human-in-loop 是架構護欄。STAGE-1（patch + 人 commit + 人 review + backtest CI）天然滿足；auto-merge 永不在 scope。

---

## 5. 成本分級（把「near-zero」變可驗證數字）

`threshold_recommend.py` 輸出層是三個純函式（`format_text/json/markdown_report`），`main()` 用 flag dispatch（`threshold_recommend.py:680-685`）。新增 `--export-patch` 與加 `format_markdown_report` **同構**；`recommended`/`delta_pct` 已算好，`abs(delta_pct)>=5.0` 過濾也已存在（`:482`）。conf.d 為單行字串值（`conf.d/db-a.yaml:6`），可 line-replace。

| Tier | 產出 | 新增碼 | 新依賴 | 除掉的 toil |
|---|---|---|---|---|
| **T1** | stdout 印 conf.d override 片段 | ~30 prod + ~20 test ≈ 50 LOC | 0 | 除「查 P95→決定值→打字」；**仍需手動 merge** |
| **T1.5** ⭐ STAGE-1 | 真正 `git apply` 的 unified diff（既有 key line-replace；新 key 退回片段） | **~100 LOC** | 0（stdlib `difflib`+`re`，對齊 #709 line-transform 教訓） | 除幾乎全部；apply→commit→既有 backtest CI 補風險報告 |
| **T2** | 直接寫 conf.d 檔 | ~120–150 LOC + idempotency/hygiene 測試 | **ruamel**（保留註解；pyyaml round-trip 會吃掉 `db-a.yaml` 註解）或 line-edit-on-disk | 同上但全自動寫檔 |
| **T3** DEFER | 自動開 Batch PR | 大（GitHub API + adapter） | — | = 要緩的重物 |

> 誠實校正：「加一個 flag」**只對 T1 字面成立**（stdout 片段）。真正除 toil 又可 apply 的是 **T1.5（~100 LOC、0 新依賴）**——這是 STAGE-1 甜蜜點。T2 因只有 pyyaml、round-trip 吃註解（違反檔案衛生 dev-rule #11），已跨進「真功能要測」範圍，不在 STAGE-1。

---

## 6. 前置票（比任何 wiring 都優先）— 建議引擎 query 可信度

`build_metric_query`（`threshold_recommend.py:342`）查 `user_threshold{key=...,tenant=...}[lookback]`；而 recording rule `tenant:alert_threshold:cpu = max(user_threshold{...})`（`rule-pack-mariadb.yaml:62-63`）證實 `user_threshold` 是**被設定的閾值值**，不是**觀測到的工作負載**。若無誤讀，則 recommend 對 P95 取的是「閾值設定本身的歷史」，而非負載歷史（負載應由 `baseline_discovery.py` 提供）。

- **decision-relevant**：若建議引擎核心 query 取錯 series，「升級成資料流上游」更該緩 → **強化 DEFER（重 adapter）**。
- 但 **STAGE-1（T1.5）仍成立**：它只把現有建議格式化成 patch，不放大引擎對錯，且人 review 會擋。
- **行動**：另開獨立票驗證/修正此 query（**在 #457 no-code scope 外**，本 issue 只記錄不修）。

---

## 7. do / defer / wontfix 對照（更新後）

| | 立論 | 反論 | now-fit |
|---|---|---|---|
| **全 do（auto-PR）** | infra 已備、與 #423 互補 | 無客戶拉力；省的只有 commit；auto-merge blast-radius；query 可信度待驗 | 弱 |
| **STAGE-1 + DEFER 重物** ✅ | 缺口僅一個 patch formatter（~100 LOC/0 dep）、繼承既有 audit-trail、拆掉雞生蛋；重 adapter 留 trigger | STAGE-1 仍是 maintainer judgment（無證據強制）；需先過前置票心理門檻 | **強** |
| **純 defer** | 最保守 | 工具續難用→silent wontfix 風險（外審 Point 3） | 中 |
| **wontfix** | 人工 review 已足 | 過早關閉 infra 已備、roadmap 已列的合理選項 | 中：太早 |

---

## 8. 外審（Gemini）take / reframe / reject ledger

| 外審論點 | 處置 | 理由（verify-don't-claim） |
|---|---|---|
| P1 global default drift / 冷啟動回饋 loop | **TAKE 概念 / REFRAME 標的** | drift 標的是 `conf.d/_defaults.yaml`（operative）+ rule pack doc-comment（鏡像），非 rule pack 當權威 → 強化 §2 reframe；列獨立 Future Work，不混進 STAGE-1 |
| P2 audit-trail 價值被低估 | **TAKE（但大半已實現）** | backtest CI 已自動貼風險報告（`backtest.yaml`）→ 價值改記為 STAGE-1 免費繼承 |
| P2 auto-merge 不對稱（調高→漏報致命） | **TAKE** | 方向正確；納入 Q5，定調 review 不可省 |
| P3 passive trigger 雞生蛋陷阱 | **TAKE 顧慮 / REFRAME 機制** | 顧慮準；但 telemetry 機制對 self-hosted 客戶不可行 → 改 STAGE-1 拆迴圈 + maintainer 主動 poll |
| P4 Partial Do（print-patch） | **TAKE 概念 / REJECT 杜撰指令** | 概念採納為 STAGE-1；但 `da-guard apply-override` **不存在**（`guard_dispatch.py` 無此 parser）→ 改 `threshold_recommend --export-patch` |

---

## 9. 給外部 adversarial reviewer 的提問（下一輪）

1. §6 的 query-series 疑慮——`threshold_recommend` 取 `user_threshold` 歷史而非負載歷史，是 bug、還是有我沒看到的 recording-rule 設計？
2. STAGE-1 該瞄準 **conf.d patch（GitOps 客戶）** 還是 **tenant-api PATCH（live UI 客戶）**？兩條 writer 都成熟。
3. T1.5「既有 key line-replace、新 key 退回片段」對「閾值首次設定」場景夠用嗎？還是該直接做 T2 寫檔？
4. drift lint（§8 P1 小版）值得**現在**獨立做嗎（債已存在、成本小）？

---

## 10. Out-of-scope（重申，無論決議）

- ❌ 本議題不 ship feature code（只 decision）；STAGE-1 若採納，另開實作票
- ❌ 不重設 #423 scope、不改 `baseline_discovery.py`
- ❌ 不擋任何 release

---

## 11. 決議落地後的檔案動作（待方向確定才執行）

- ✅ `CHANGELOG.md` `## [Unreleased]`：已記 DEC-F definitive 結果
- ✅ `roadmap-future.md`「維運自動化」條目：「資料流閉環評估」→ 已改為 STAGE-1 採納 + DEFER trigger + REJECT
- ✅ 本文 `status: draft → accepted` + 頂部決議框
- ℹ️ planning archive（v2.8.0 planning §10 DEC-F）為 **maintainer-local**，不在本 repo；**in-repo 決議 SSOT = 本評估書**（`planning-id-mapping.md` 僅 legacy→TRK 翻譯表，DEC 非其 namespace，不登錄）
- ⬜ GitHub follow-up tickets（待開）：(1) 前置票 — `threshold_recommend` query series 驗證（§6，最優先）；(2) STAGE-1 實作票 — `--export-patch` T1.5；(3) Future Work — drift lint / global default drift 報表 / portal inline（各 defer-with-trigger）；(4) #457 留言記 R0 結論後 close
