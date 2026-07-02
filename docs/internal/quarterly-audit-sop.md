---
title: "季度 Rule-corpus Drift 稽核 SOP"
tags: [internal, dx, governance, sop]
audience: [maintainers]
version: v2.9.1
verified-at-version: v2.8.1
lang: zh
---

# 季度 Rule-corpus Drift 稽核 SOP（TRK-307）

> Vibe 的規則語料散在四處（dev-rules / pre-commit hooks / vibe skills / memory feedback 卡），會隨時間老化、重複、漂移。本 SOP 每季跑一次 `audit_rules_drift.py` 產 drift report，**人工**裁決後收斂。與 [`hook-vs-skill-coverage.md`](hook-vs-skill-coverage.md)（owner 矩陣）互補：矩陣是 SSOT，本稽核是定期 drift 偵測。

## 何時跑

- **每季一次**（建議對齊 milestone 收尾，如 epic #570 的 TRK-310 瘦身前先跑一次）。
- 新增 ≥3 個 hook / skill / feedback 卡後，或感覺規則開始重複時，可臨時跑。
- **不入 CI**（季度頻率不值得每 PR 跑；且完整稽核需 maintainer 機器上的 `~/.claude` memory 目錄，CI 無）。

## 怎麼跑

```bash
make audit-rules
# 或直接： python3 scripts/ops/audit_rules_drift.py
# 只看不寫檔： python3 scripts/ops/audit_rules_drift.py --stdout
```

report 寫到 `docs/internal/audit-reports/rules-drift-YYYY-MM.md`（atomic write，LF）。在 maintainer 機器上跑才會含 feedback 卡檢查（memory 目錄在 `~/.claude/...`）；CI / 無 memory 環境會跳過 §3-5 並註記。

## 報表七段怎麼讀

| 段 | 訊號 | 處理 |
|---|---|---|
| 語料盤點 | 各來源數量 | 對照上次 report 看成長趨勢 |
| 1. Count reconciliation | 🕳️ = hook 切分數 vs CLAUDE.md 宣告不符 | 校正 CLAUDE.md（**計數一律以 YAML parse 為準，勿用 grep**——grep 會配到註解行） |
| 2. Hook ↔ dev-rule 缺口 | 👁️ 顯式 reviewer-only / 🕳️ 未提機械防線 | 對照 `hook-vs-skill-coverage.md` §7 漏接是否已收錄；真缺則評估補 lint |
| 3. 重複候選 | 🔁 相似度 ≥ 0.60 | 人工判斷是否合併 / 下放；**勿自動刪**（speculative 清理會誤刪仍有效規則） |
| 4. Feedback cross-ref | 🕳️ orphan / broken ref | 補進 `MEMORY.md` index 或修連結 |
| 5. Stale feedback | ⏳ > 120 天未更新 | 確認是否仍適用；已升 CLAUDE.md root 的可考慮下放 deep-dive |

## 附帶巡檢：上游 skill-system FR（TRK-309）

跑季度 audit 時，順手巡檢 [`skill-system-feature-requests.md`](skill-system-feature-requests.md) 的 FR-01~06：上游（Anthropic / Cowork）是否已**靜默**解決任一項（skill-spec 改版常不大肆宣傳，該表的被動 trigger 抓不到）？已解決的標 ✅ / 移除。此步把那份「upstream 願望表」綁進 recurring 主動流程，避免它變成不準確的技術債。

## 編輯 always-on context 後：recall test（TRK-310 驗證的方法）

動到 always-on tier-1 context（CLAUDE.md 高頻地雷 / dev-rules Top 4 / skill 清單）後——尤其**為瘦身而壓縮**時——跑 recall test 驗死線沒被埋：

1. 開**乾淨 subagent**（`Agent` general-purpose），只餵改後的 CLAUDE.md，要它窮舉所有 must-follow / ⛔ 規則（禁讀其他檔、禁腦補）。
2. 比對 ground truth（5 條高頻地雷 + dev-rules Top 4）：**critical 規則須 100% 被抽出**；漏任一條 = 壓過頭，回補 salience（獨立 bullet / ⛔ / bold）。
3. subagent 標「模糊 / 半條」的多半是被 inline 的 advisory——可接受（**salience 分級**：critical 保持 atomic、advisory 可壓）。

**為何用 recall 不用密度數字**：行數 / 字元是誤導 proxy（TRK-310 實測 CLAUDE.md 行數 −1 但 token +19%）。該守的是「critical 規則冷讀 100% 可抽出」，與總大小無關。密度加權公式（如外審提的 ACT 40/40/20）是杜撰精確度；recall 是 pass/fail 實測。

## vibe-* skill 汰除（dead-weight 防治；epic #570 retrospective）

每季 audit 逐一檢查每個本地 `vibe-*` skill **過去一季是否在其領域內被實際觸發**（subagent-review: multi-file PR review；release: 發版；brainstorm: 設計討論；workflow/dev-rules/playbook-nav: 日常）。**連續 2 季 0 觸發 = dead weight，audit 時強制刪除**（對齊 `feedback_speculative_drift_prefer_remove`）。觸發案例寫進 CHANGELOG / PR body 當佐證。**理由**：epic #570 交付 3 個新 skill 但收尾時 subagent-review/release **0 觸發**、brainstorm 僅 1（Gap A）；無問責機制會養出沒人用的 skill。

## 文件 staleness 防線：lint 層 vs dogfood 層（#141）

客戶導入/安裝文件會悄悄 stale（命令語法、版號、路徑、workload 類型隨 code 漂移）。防線**分兩層**，邊界原則同 [`hook-vs-skill-coverage.md`](hook-vs-skill-coverage.md)：**deterministic 的交給 lint、判斷題留給人工 dogfood**。

### 機械層（lint，CI/pre-commit 自動擋）

| Lint | 抓的 staleness 類 | 燒過 |
|---|---|---|
| `validate_docs_versions.py`（`check_release_tag_currency`）| 版號字面：`tools/vX` / `--set image.tag=vX` / `da-* --version` 輸出 vs 當前 release | TB-F1 |
| `check_doc_k8s_refs.py` | `k8s/**` manifest 路徑存在 + doc 宣稱的 workload kind vs 實際 `k8s/`（`kubectl ... statefulset/deployment <元件>`）| TB-F4 |
| `check_doc_datools_cmds.py` | 文件裡 `da-tools guard/parser/batch-pr` 子命令 vs dispatcher 實際集 | F3（`guard /conf.d`）|

> **刻意沒做的廣義版**（記取教訓，別重造）：「掃所有 repo 路徑是否存在」「驗所有 `da-tools <任意命令>`」實跑都是 **~88% false-positive**（文件充斥 example/aspirational：`my-db.yaml` / `describe-tenant` / 「PR-3 預定加」的工具）—— 廣義 lint = 你燒過的 reactive-whack-a-mole 反模式。三支都**收斂到真 artifact 才有的窄域**（k8s/、binary-wrapper 子命令）。

### 人工層（dogfood，lint 抓不到的判斷題）

機械抓不到、**只能靠定期 cold-walk dogfood** 的類別：

- **「宣稱 future 但已出貨」**（TB-F2：文件說 cosign「後續迭代」但已隨 release 發佈）— 語義，無法 lint。
- **UX 過度警告 / 措辭**（Track A F1：WSL2 警告比實際保守）— 判斷題。
- **flag-level 漂移**（`--config-dir` 改名）— Go binary flag 無法從 python 內省。

### Dogfood 方法（per-run log 不 commit，方法論住這裡）

每次（onboarding surface 變動 / 發版前）以**新使用者冷視角只照文件**走一遍，三步：

1. **Cold-walk**：清環境、只照文件跑（da-tools 安裝 + config 驗證 + try-local / 安裝路徑），卡住即記 friction。
2. **對抗式全 repo 同類掃描**：撞到一個 bug（如某檔 stale 版號）後，**grep 整個 `docs/` 找同類**——初版只修撞到的、漏網的靠這步補（#141 Track B 這步多揪出 3 檔）。逐筆分 real-bug / example / aspirational（別誤殺 example）。
3. **Independence baseline**：算「≥80% 步驟只照文件可自助完成」；fail 的步驟 = 真 doc bug，同 cycle 修。

產出歸屬：**doc 修正 → PR**、**residue/TODO → 對應 issue**、**方法 → 本節**。per-run runbook **不 commit**（archive/ 是凍結歷史、per-run log 長期價值低；#141 移除了兩份）。

## 裁決原則（重要）

- **只產 report，不自動修改**。所有合併 / 刪除 / 下放都人工決定 — speculative 自動清理會誤刪仍有效的規則。
- **重複 ≠ 該刪**：trailer 規則「4 層」、sed-i「5 層」是 intentional safety redundancy（見 `hook-vs-skill-coverage.md` §7 Overlap）。先問「這層冗餘是不是故意的」再動。
- **count 校正一律 YAML parse**：本工具用 `yaml.safe_load` 數 hook stage，不用 grep。TRK-307 上線首跑即抓出初版（PR #582）用 grep 配到 `jsx-babel-check-strict-linecount` 註解行造成的 50/14 誤算（真值 51/13）。

## 報表保存

每季 report 留在 `docs/internal/audit-reports/`，檔名 `rules-drift-YYYY-MM.md`。保留歷史以看趨勢；超過 1 年的可歸檔。

## 關聯

- 工具：`scripts/ops/audit_rules_drift.py`（`make audit-rules`）
- owner 矩陣 SSOT：[`hook-vs-skill-coverage.md`](hook-vs-skill-coverage.md)
- 互補工具：`anthropic-skills:consolidate-memory`（只掃 `~/.claude` memory；本稽核補 repo 內規則語料）
- 規範：[`dev-rules.md`](dev-rules.md)
- epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-307
- 文件 staleness 防線（lint L1–L4 + dogfood）：[#141](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/141)；lints 在 `scripts/tools/lint/`（`validate_docs_versions.py` / `check_doc_k8s_refs.py` / `check_doc_datools_cmds.py`）
