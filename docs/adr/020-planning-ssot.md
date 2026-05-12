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
> **EN mirror**：本 ADR 已 `Accepted`；EN 翻譯 tracked under [issue #409](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/409)（與 ADR-019 對齊的雙語策略）。

## 狀態

✅ **Accepted**（PR [#375](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/375)，v2.8.0）

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
id: TRK-042 | S#74 | ADR-019    # 三 namespace 共存，見下方 namespace policy
tracking_kind: tech-debt | feature | dx | regression | adr | sprint
status: proposed | accepted | in-progress | done | abandoned | superseded
domain: tenant-api | exporter | portal | docs | ci | rule-packs | ...
supersedes: [TRK-005, TRK-010]  # optional：解決哪些舊條目
superseded_by: TRK-050          # optional：被誰取代
pr_ref: 375                     # done 後填，多筆用 list
target_version: v2.9.0          # optional
created_at: 2026-04-15
updated_at: 2026-05-10
owner: poyu                     # optional：誰負責
---
```

**`tracking_kind`** 是專案私有 enum，避免 false-positive 撈到含 `id:`/`status:` 的無關 doc（如 OpenAPI spec、tutorial）。

### Namespace Policy（三 namespace 共存）

本 ADR 採三 namespace 並存策略：

| Namespace | 用途 | 為什麼不合併 |
|---|---|---|
| **TRK-NNN** | 統一 debt/regression/dx tracking。**取代既有 TD-NN / HA-NN / REG-NN** 三個分散 namespace | 三者本質同類（都是「該修還沒修」狀態追蹤），分散是歷史包袱。AI/人類各自記三個 prefix 沒價值 |
| **ADR-NNN** | 架構設計決策史 | ADR 是 **永久 design history**，不是 backlog。已被多處 user-facing doc / commit / external citation 引用（如 [README.md](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/README.md) 引用 ADR-007/ADR-018/ADR-019 等）；rename 等於重寫設計史，redirect mapping 永遠維護。語意上 ADR 與 TRK 也根本不同：ADR 是「**這個決策當時為什麼**」，TRK 是「**這個工作 done 了沒**」 |
| **S#NNN** | Sprint planning ledger（時序性、跨 sprint 的階段標記） | S# 對應 sprint 內 work item，本質是時序 + 階段，跟跨 sprint 的持續債務不同。混入 TRK 會稀釋語意。Sprint 結束後若條目仍 open 才 promote 為 TRK（done 的 sprint 條目歸檔即可，不必 rename） |

### TD-NN / HA-NN / REG-NN → TRK-NNN 遷移

> **狀態（v2.8.1-dx-interim）**：chunk 1 已落地 — [`docs/internal/planning-id-mapping.md`](../internal/planning-id-mapping.md) 與全 repo 批次 rewrite 完成。實際 footprint 為 ~73 處（前估 ~207 處，重 grep 後修正；見 [issue #379 footprint correction comment](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379)）。本 ADR 文內 `TD-NN` / `HA-NN` / `REG-NN` 字樣**刻意保留**——它們是政策論述本身的引用，而非追蹤項目參照。

- **舊 ID 對映**：見 [`docs/internal/planning-id-mapping.md`](../internal/planning-id-mapping.md)（含完整 TECH-DEBT / TD / HA / REG → TRK 對映表 + redirect 說明 + 三段編號分區邏輯）
- **既有引用 rewrite**：~73 處批次替換為 TRK（TD-022 → TRK-222、HA-11 → TRK-011、REG-004 → TRK-104 等；分區範圍見 mapping doc）
- **CHANGELOG-archive.md + docs/internal/archive/ 不動**：pre-v2.2.0 引用作歷史保留，redirect doc 解釋舊 ID 在現代是 TRK-NNN
- **新條目從 v2.8.1 起一律 TRK-NNN**（新分配從 TRK-300+ 開始）

### Frontmatter 上 ADR / S# 的 id 寫法

ADR 與 S# 的 frontmatter `id:` 維持原 namespace：

```yaml
# ADR
id: ADR-020
tracking_kind: adr

# Sprint
id: S#74
tracking_kind: sprint

# 統一 backlog (新)
id: TRK-042
tracking_kind: tech-debt | regression | dx
```

`generate_planning_index.py` 對三 namespace 一視同仁，但在 index UI 分組顯示。

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

### 替代方案 A：**全** consolidate 到單一 namespace（含 ADR）

**早期設計提案 + Owner 初步傾向**。優點：AI 與人類只需熟悉一個 prefix。缺點：

- 79 處 TD-NN + 128 處 HA-NN/REG-NN + 232 處 S#NN 引用要 rewrite — 共 ~440 處
- 19 份 ADR 重新編號 — **最高風險**：
  - ADR 已被 user-facing docs（[README.md](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/README.md)、[architecture-and-design.md](../architecture-and-design.md)、各 component README）多處引用作 design citation
  - ADR 也已在外部 commit messages / 客戶看的 changelog / GitHub issue 內被引用為 design history reference
  - rename 等於重寫設計史，redirect mapping 須永久維護
- ADR 與 backlog tracking 概念上不同類（design history vs work-in-progress tracking），合併稀釋兩者語意
- 真實成本估算（grep 後重算）：~35-38h（不 skip pre-v2.2.0）/ ~30-35h（skip pre-v2.2.0；savings 僅 3-5h，因絕大多數引用已是 post-v2.2.0）

**結論**：本 ADR 採 **Option C refined hybrid**，TD/HA/REG 合併為 TRK 但 ADR 與 S# 各自保留。原因是 ADR 不該動 + S# 概念不同類。Option A 與 hybrid 的成本差約 8-10h，全用在 ADR rename 上 — 該 8-10h 換來的「ADR 也進 TRK」反而是設計上的退步。

### 替代方案 A'：保留所有既有 namespace（TD/S#/HA/REG/ADR），不引入新 TRK

**本 ADR 起草早期傾向**。優點：零 rewrite 成本，純加 frontmatter。缺點：

- TD / HA / REG 三個 namespace 本質同類（都是 debt/regression tracking），長期共存是歷史包袱
- AI agent 與新 contributor 須學會 4 個 backlog-like prefix（TD/HA/REG/REG），對 onboarding 不友善
- 不解決核心問題：fragmentation 不只是「散在不同檔」，也是「同一概念三個別名」

**結論**：成本（~25h）僅比 Option C（~27h）省 2h，但放任 namespace fragmentation 永久存在，不划算。

### 替代方案 B：Polling-based Stale Check（無 active sync）

「每週 cron 跑掃描，找『標 done 但無 PR ref』『標 in-progress 但 90 天無 commit』的條目」。

**問題**：被動、漂移已發生才發現。`Resolves TD-NN` 寫在 PR body 是 active 點，CI 階段就驗最廉價。

**結論**：採 active 為主，polling 作 secondary safety net（每月 cron 列 stale 條目給 maintainer 季度 review）。

### 替代方案 C：硬編 source files 清單

`generate_planning_index.py` 寫死 8 個檔案路徑掃。

**問題**（adversarial review 點出）：下個月新建 `docs/internal/security-backlog.md` 會被遺忘加入清單，新 source 對 AI 隱形。

**結論**：採 discovery-based（glob + frontmatter 過濾）。

## 實作計畫

| 階段 | 內容 | effort |
|---|---|---|
| 1. ADR 與 spec ship | 本 ADR + frontmatter spec 定稿 | 4h |
| 2. **TD/HA/REG → TRK 對映 + rewrite** | 建 mapping table（`docs/internal/planning-id-mapping.md`）+ scan repo ~207 處引用批次替換 + redirect doc | 9-10h |
| 3. 工具實作 | `generate_planning_index.py` + `check_planning_status_sync.py` | 12h |
| 4. Source migration | 既有 backlog files 加 frontmatter，新 entries 一律 TRK-NNN | 6h |
| 5. CLAUDE.md 起手式 + dev-rules.md 收編 + commit-convention.md 提到 TRK | 強制 AI 必讀 index | 2h |
| **Total** | — | **~33-34h** |

> 注意：因 grep 重算後實際引用數比初估的 60h 少很多，且 ADR/S# 不動省下大量 rename 成本，實際 Option C 成本落在 ~27-34h 區間（取決於 mapping table 多細、redirect doc 多嚴謹）。
>
> Pre-v2.2.0 引用（CHANGELOG-archive.md / docs/internal/archive/）**不動** — redirect doc 內標明「歷史 ID 在現代為 TRK-NNN」即可，savings 約 3-5h。

## 後果（Consequences）

### 正面

- AI agent 不再 fragment：起手式讀 index 就有全景
- contributor onboarding：新人看 index 知道哪些事 in-flight、誰負責
- maintainer 季度 review：從 8 處收斂變從 1 處 filter
- PR review：CI 強制 status sync 杜絕「修了但沒回頭 close」漂移
- TD/HA/REG → TRK 統一後，backlog tracking 從三 prefix 收斂為一個，AI 與人都少記憶 2 個
- ADR 與 S# 各自保留語意純度（design history vs sprint planning），不被混入 backlog tracking

### 負面

- 一次性 rewrite 成本（~9-10h）：207 處引用批次替換 + 對映表
- redirect doc 永久維護：CHANGELOG-archive 等歷史檔的舊 ID 須在 redirect 內查到 TRK 對映
- 既有 ~50 條 active backlog entries 需要分批加 frontmatter（一次性投資）
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
