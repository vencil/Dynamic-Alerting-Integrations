---
title: "季度 Rule-corpus Drift 稽核 SOP"
tags: [internal, dx, governance, sop]
audience: [maintainers]
version: v2.8.1
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
