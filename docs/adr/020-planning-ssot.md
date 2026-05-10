---
title: "ADR-020: Planning SSOT — Frontmatter Contract + Discovery-based Index"
tags: [adr, dx, planning, ai-agent, governance]
audience: [platform-engineers, contributors, ai-agents]
version: v2.7.0
lang: zh
---

# ADR-020: Planning SSOT — Frontmatter Contract + Discovery-based Index

> 跨檔分散的計畫追蹤（tech-debt / dx-backlog / known-regression / future-roadmap / sprint planning）統一治理。
> 對 AI agent 解決 context fragmentation；對人類 contributor 提供單一索引入口。
>
> **EN mirror**：本 ADR 仍在 `Proposed` 階段；待 `Accepted` 後 ship `020-planning-ssot.en.md`（與 ADR-019 對齊的雙語策略）。

## 狀態

🟡 **Proposed**（PR #TBD，2026-05-10 起草）

## 背景

專案的「未來計畫 / 已知問題 / 進行中工作」目前散落在至少 8 處：

| 來源 | 範圍 | 主要 ID 形式 |
|---|---|---|
| `CHANGELOG.md [Unreleased]` | in-flight 工作條目 | 內聯描述 |
| `docs/internal/dx-tooling-backlog.md` | DX 工具待辦 | `TD-NNN` |
| `docs/internal/frontend-quality-backlog.md` | 前端品質改善 | mixed |
| `docs/internal/v2.8.0-planning.md` + archive | sprint planning ledger | `S#NNN` |
| `docs/design/roadmap-future.md` | 長期 roadmap | 內聯描述 |
| 各 ADR 的「Future Work」段 | 架構演進方向 | `ADR-NNN` |
| code 內 `// TECH-DEBT:` / `// FIXME:` / `// REG:` 註解 | 程式碼層債務 | mixed |
| `flaky-tests.yaml` | 測試 flake registry | `HA-NN` 引用 |

### 問題：AI Agent Context Fragmentation

對 AI agent 的具體危害（在 PR #375 retrospective 中觀察到）：

1. **不知 source 全集** — agent 在 CHANGELOG `[Unreleased]` 計畫某事，但其實 `dx-tooling-backlog.md` 已有同樣 entry，造成重複設計或衝突
2. **不知狀態同步** — 某項工作已 done 但只改 code 沒回頭 close 對應 backlog entry，agent 讀到陳舊資訊以為仍待辦
3. **不知 cross-ref 漂移** — 改了 source A 的 status，沒同步 source B 引用 A 的位置
4. **無法回答「現在什麼 in-flight？」** — agent 須讀 8 處才能合併出全景

### 對人類 contributor 的問題

- 新 contributor 不知道工作要登記在哪一份 backlog
- maintainer 季度 review 要跨 8 個 source 收斂
- 「為什麼 PR 解決 issue X 但 backlog 還掛著 X」這類 inconsistency 一年累積一次大掃除

## 決策

### 三層設計

**Layer 1：Frontmatter Contract（每個 planning entry 必填）**

每個 planning item（不論在哪份 source file）都要在自身 markdown section（或 yaml block）帶以下 frontmatter：

```yaml
---
id: TD-030 | S#74 | HA-11 | REG-7 | ADR-019  # 用既有 namespace，不引入 TRK-
tracking_kind: tech-debt | feature | dx | regression | adr | sprint
status: proposed | accepted | in-progress | done | abandoned | superseded
domain: tenant-api | exporter | portal | docs | ci | rule-packs | ...
supersedes: [TD-005, TD-010]  # optional：解決哪些舊條目
superseded_by: TD-050           # optional：被誰取代
pr_ref: 375                     # done 後填，多筆用 list
target_version: v2.9.0          # optional
created_at: 2026-04-15
updated_at: 2026-05-10
owner: vencil                   # optional：誰負責
---
```

**`tracking_kind`** 是專案私有 enum，避免 false-positive 撈到含 `id:`/`status:` 的無關 doc（如 OpenAPI spec、tutorial）。

**Layer 2：Discovery-based Index Generator**

`scripts/dx/generate_planning_index.py`：

- Glob 掃 `docs/**/*.md` + 既有的 `flaky-tests.yaml` + code 內 `// TECH-DEBT(id=...,status=...,tracking_kind=...)` 標準註解
- 過濾條件：必須有 `tracking_kind:` 欄位且值在 enum 內
- 輸出 `docs/internal/planning-index.md`，多維度 sort（by status / domain / target_version）
- 自動連結到 source file + line number
- pre-commit auto-stage hook，每 commit 重產

**重要設計選擇**：source-of-truth 是**各 source file 內的 frontmatter**，不是 index。Index 是 derived view，永遠可重新生成。修改某項 status 必須改 source，不改 index。

**Layer 3：Active CI Sync Check**

`scripts/tools/lint/check_planning_status_sync.py`：

- 從 GitHub PR body parse `Resolves TD-NN` / `Closes S#NN` / `Fixes HA-NN` 等 conventional close 標記
- 對每個被 resolve 的 ID：
  - 驗對應 entry 的 frontmatter `status:` 是否在這個 PR 內變成 `done`
  - 驗 `pr_ref:` 是否填了當前 PR number
- 不通過 → CI 黃燈警告 + PR comment 提醒
- 連續未補（3 PR 內未 fix） → 紅燈擋 merge

CI 機制觸發時機：pre-merge GitHub Actions check（讀 PR body via `${{ github.event.pull_request.body }}`），或 pre-push hook 本地 dry-run。

#### ⚠️ Implementation gotcha：Regex 邊界與大小寫

Parse PR body 抓 close-marker 的 regex **必須** 同時滿足兩條件：

1. **Word boundary `\b`**：避免 `TD-1` 誤匹配到 `TD-10` / `TD-100` / `TD-1000`
2. **Case-insensitive**：開發者寫 `Resolves` / `resolves` / `RESOLVES` 都該認

**正確 regex**：

```python
CLOSE_MARKER_RE = re.compile(
    r"(?i)(?:resolves|closes|fixes|fix)\s+(TD-\d+|S#\d+|HA-\d+|REG-\d+|ADR-\d+)\b"
)
```

注意 `(?i)` flag + namespace 後 `\b` 結尾。`S#` 的 `#` 不是 word char 所以 `\b` 在 `\d+` 後而不是 namespace 後。

**測試 case 必須涵蓋**：

```python
# Should match
"Resolves TD-30"           → TD-30
"resolves td-030"          → TD-030 (case-insensitive)
"Closes HA-11 and S#74"    → HA-11, S#74

# Should NOT cross-match
"Resolves TD-1 (not TD-10)"   → only TD-1
"See TD-100 below"            → no match (no close marker prefix)
```

### CLAUDE.md 必讀宣告

CLAUDE.md 起手式段加入：

```markdown
**AI agent 起手式必讀**：
[`docs/internal/planning-index.md`](docs/internal/planning-index.md) — 跨檔合併的計畫
全景索引。動任何 backlog / debt / regression 相關任務前先看，避免規劃既有條目。
```

## 為什麼不用其他方案

### 替代方案 A：全 consolidate 到單一 `docs/tracking/` + 新 `TRK-NNN` namespace

**Gemini 提案**。優點是 AI 完全只需讀一處。缺點：

- ~150 個既有 ID（TD/S#/HA/REG/ADR）需重新編號
- CHANGELOG / commit messages / code comments 大量引用要 rewrite 或建 redirect mapping
- contributor 認知負擔（新 namespace 要熟悉）
- 遷移成本估 60h+ vs 本 ADR 方案 25h

**結論**：cost-benefit 不划算。AI 看到 `TD-030` 跟看到 `TRK-030` 一樣陌生，重點是有沒有索引能跳轉，不是 ID prefix 統一。

### 替代方案 B：Polling-based Stale Check（無 active sync）

「每週 cron 跑掃描，找『標 done 但無 PR ref』『標 in-progress 但 90 天無 commit』的條目」。

**問題**：被動、漂移已發生才發現。`Resolves TD-NN` 寫在 PR body 是 active 點，CI 階段就驗最廉價。

**結論**：採 active 為主，polling 作 secondary safety net（每月 cron 列 stale 條目給 maintainer 季度 review）。

### 替代方案 C：硬編 source files 清單

`generate_planning_index.py` 寫死 8 個檔案路徑掃。

**問題**（Gemini 對抗 reviewer 點出）：下個月新建 `docs/internal/security-backlog.md` 會被遺忘加入清單，新 source 對 AI 隱形。

**結論**：採 discovery-based（glob + frontmatter 過濾）。

## 實作計畫

| 階段 | 內容 | effort |
|---|---|---|
| 1. ADR 與 spec ship | 本 ADR + frontmatter spec 定稿 | 4h |
| 2. 工具實作 | `generate_planning_index.py` + `check_planning_status_sync.py` | 14h |
| 3. Source migration | 8 處 source 全加 frontmatter（分批 PR）| ~10h |
| 4. CLAUDE.md 起手式 + dev-rules.md 收編 | 強制 AI 必讀 index | 1h |

## 後果（Consequences）

### 正面

- AI agent 不再 fragment：起手式讀 index 就有全景
- contributor onboarding：新人看 index 知道哪些事 in-flight、誰負責
- maintainer 季度 review：從 8 處收斂變從 1 處 filter
- PR review：CI 強制 status sync 杜絕「修了但沒回頭 close」漂移
- 既有 namespace（TD/S#/HA/REG/ADR）保留，無 rename 成本

### 負面

- 既有 ~150 條 entries 需要分批加 frontmatter（一次性投資）
- 每條新 backlog 寫作摩擦增加（需填 7-10 個 frontmatter 欄位，但有 template）
- discovery-based 對「不該被 index」的 doc（含巧合相符 frontmatter）需要 `tracking_kind:` enum 嚴格化
- AI 必讀宣告增加 CLAUDE.md context 負擔（但 index 文件本身可短，~200 行 table）

### 中性

- 新 lint 多一條（`check_planning_status_sync.py`）— 屬 (b) class（discovery + 規則驗證），歸進 lint-policy.md 治理
- 季度 maintainer review 從「審查每個 source 是否同步」改為「審查 index 內 stale 條目」，工作量類似但聚焦

## Future Work

- 若 AI 必讀 index 仍不夠，考慮把 index 額外注入 `.claude/skills/vibe-planning-nav/SKILL.md` 讓 skill 觸發時直接帶入
- domain admin 級別 backlog scope（讓某 domain 的 owner 看自己 domain 的 in-flight）— v2.9.0 評估
- 自動關聯 ADR ↔ 解決的 TD：當 ADR ship，自動掃 ADR body 找「解決 TD-NN」字樣 + cross-link

## 關聯

- 本 ADR 的觸發來自 PR #375 retrospective 中對 AI Agent Context Fragmentation 的識別
- 與 [lint-policy.md](../internal/lint-policy.md)（同 PR ship）的 (b) class lint 治理是配套：本 ADR 的 sync check 是新 (b) class lint
- 與 [dev-rules.md](../internal/dev-rules.md) #4 Doc-as-Code 同源（both 強調自動化驗證 vs 人工記憶）
