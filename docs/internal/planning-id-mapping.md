---
title: "Planning ID Mapping — Legacy → TRK Redirect"
tags: [internal, dx, planning, redirect]
audience: [contributors, ai-agents]
version: v2.8.1
lang: zh
---

# Planning ID Mapping — Legacy → TRK Redirect

> **本文件用途**：[ADR-020](../adr/020-planning-ssot.md) 採 **Option C refined hybrid**——把舊的 `TECH-DEBT-NNN` / `TD-NN` / `HA-NN` / `REG-NNN` 四個 namespace 統一為單一 `TRK-NNN`。本文是 **redirect 表**：當你在 commit / PR / 文件 / external citation 看到舊 ID，到這裡查對應的現代 `TRK-NNN`。
>
> **這不是 backlog**，只是 ID 翻譯表。Backlog 本體（current status / pr_ref / owner）放在各自 source frontmatter，最終透過 `scripts/dx/generate_planning_index.py`（issue [#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 2a）匯出 `planning-index.md`。

## Namespace 政策回顧

| Namespace | 用途 | v2.8.1-dx-interim 後狀態 |
|---|---|---|
| **TRK-NNN** | 統一 debt / regression / dx tracking | **唯一新增進入點**（v2.8.1+ 一律 TRK） |
| **ADR-NNN** | 架構設計決策史 | 保留（不參與本 mapping）|
| **S#NNN** | Sprint planning ledger | 保留（不參與本 mapping）|
| ~~`TECH-DEBT-NNN`~~ | (legacy) | 凍結；本表查對應 TRK |
| ~~`TD-NN`~~ | (legacy 簡寫) | 凍結；本表查對應 TRK |
| ~~`HA-NN`~~ | (legacy) | 凍結；本表查對應 TRK |
| ~~`REG-NNN`~~ | (legacy) | 凍結；本表查對應 TRK |

## 編號分區

為了讓 grep / review 一眼可辨原 namespace，TRK 編號採三段分區：

| 區段 | 來源 | 範例 |
|---|---|---|
| **TRK-001 ~ TRK-099** | `HA-N` 序列 | `HA-11` → `TRK-011` |
| **TRK-100 ~ TRK-199** | `REG-NNN` 序列 | `REG-004` → `TRK-104` |
| **TRK-200 ~ TRK-299** | `TECH-DEBT-NNN` / `TD-NN` 序列 | `TECH-DEBT-005` → `TRK-205`、`TD-022` → `TRK-222` |
| **TRK-300 +** | **post-migration 新分配** | — |

> `TECH-DEBT-NNN` 與 `TD-NN` 是同一個 namespace 的長短形（v2.7.x 之後簡寫為 `TD-`，數字編號連續），同號 alias 合併到同一個 TRK：`TECH-DEBT-022` ≡ `TD-022` → `TRK-222`。
>
> 字母 suffix（e.g. `TD-030a`, `TD-030z`, `TD-032e`）保留，遷移為 `TRK-230a`, `TRK-230z`, `TRK-232e`。

## Mapping 表

### HA-N → TRK-001 ~ 018（DX hardening / automation tracking）

SOT 在 [`dx-tooling-backlog.md`](dx-tooling-backlog.md)。

| Legacy | TRK | 主題 |
|---|---|---|
| HA-1 | TRK-001 | `check_noqa_hygiene.py` noqa/nosec 必要性驗證 |
| HA-2 | TRK-002 | `make test-impact` 變更影響測試自動縮減 |
| HA-3 | TRK-003 | Pre-commit hook CI gate |
| HA-4 | TRK-004 | Lint tool self-test framework（negative fixtures）|
| HA-5 | TRK-005 | `check_test_isolation.py` 測試隔離驗證 |
| HA-6 | TRK-006 | Skip budget CI gate（`make test-skip-audit`）|
| HA-7 | TRK-007 | Lint test coverage 補齊 |
| HA-8 | TRK-008 | CI ignore 文件化與 test-map 更新 |
| HA-9 | TRK-009 | Coverage source 一致性 lint |
| HA-10 | TRK-010 | Flake 自動重試 CI Policy |
| HA-11 | TRK-011 | Fake-Clock 注入（根因修復 Go 時間相依測試）|
| HA-12 | TRK-012 | ADR / 內部連結檔名一致性 Lint |
| HA-13 | TRK-013 | Spoke 文件 Freshness Gate |
| HA-14 | TRK-014 | FUSE-side Git Write 防護 Wrapper |
| HA-15 | TRK-015 | Session 起手式 PATH+PATHEXT Smoke Test |
| HA-16 | TRK-016 | CHANGELOG 計數一致性 Lint |
| HA-17 | TRK-017 | Desktop Commander 長命令 Watchdog Wrapper |
| HA-18 | TRK-018 | `engineering:testing-strategy` Skill 驅動的測試設計還債 |

### REG-NNN → TRK-101 ~ 199（產品 / portal regression registry）

> `known-regressions.md` 於 [Session #16 radical-delete policy phantom-deleted](../../CHANGELOG.md)，REG 條目分散於各 PR commit / playbook 引用。

| Legacy | TRK | 主題 |
|---|---|---|
| REG-001 | TRK-101 | (reserved placeholder — 從未實際登錄) |
| REG-003 | TRK-103 | `docs/interactive/changelog.html` 缺 v2.1-v2.6 timeline（v2.8.0 resolved，CHANGELOG.md Phase .a SSOT bundle）|
| REG-004 | TRK-104 | portal-safe hrefs：絕對根路徑 `href="/foo"` 在 portal sub-path 部署會 404；`assertNoAbsoluteRootHrefs` helper 防守 |

### TECH-DEBT-NNN / TD-NNN → TRK-201 ~ 299（platform tech debt）

| Legacy | TRK | 主題 |
|---|---|---|
| TECH-DEBT-001 | TRK-201 | (early-era debt tracking — 參見 ADR-014/015/016 引用脈絡) |
| TECH-DEBT-002 | TRK-202 | (early-era) |
| TECH-DEBT-003 | TRK-203 | (early-era) |
| TECH-DEBT-005 | TRK-205 | palette 殘留導致 dark mode 斷層（ADR-016 cited）|
| TECH-DEBT-006 | TRK-206 | scrollable container axe-core a11y |
| TECH-DEBT-007 | TRK-207 | design-system token canonical 值校正（次要文字 `#475569`）|
| TECH-DEBT-008 | TRK-208 | form element accessible name CRITICAL violation |
| TECH-DEBT-009 | TRK-209 | (dev-rules 縮寫引用 `-009` — early-era) |
| TECH-DEBT-011 | TRK-211 | (dev-rules 縮寫引用 `-011` — Day 5 runtime axe a11y violation) |
| TECH-DEBT-012 | TRK-212 | (dev-rules 縮寫引用 `-012` — 同上) |
| TECH-DEBT-016 | TRK-216 | MetricCard subStyle 雙背景 dark mode |
| TECH-DEBT-017 | TRK-217 | WatchLoop time.Sleep flake → FakeClock 結構性修復（v2.8.0 PRs #363–#369）|
| TECH-DEBT-021 | TRK-221 | `make api-docs` Makefile target / tenant-api swag → OpenAPI spec pipeline（v2.8.0 [#226](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/226)）|
| TECH-DEBT-022 ≡ TD-022 | TRK-222 | schemathesis 契約測試（[issue #231](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/231)）|
| TD-028 | TRK-228 | `/api/v1/me` JSON wire shape nil-vs-empty-array drift（[issue #242](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/242)）|
| TD-030 | TRK-230 | Portal ESM build + Vitest（[issue #247](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/247) Option C sweep）|
| TD-030a | TRK-230a | sub-PR a — foundation |
| TD-030b | TRK-230b | sub-PR b — first wave migration |
| TD-030z | TRK-230z | sub-PR z — `jsx-loader.html` 最終下架 |
| TD-032 | TRK-232 | Portal E2E coverage push（v2.8.0 LL）|
| TD-032e | TRK-232e | sub-PR e — debug iteration |
| TD-033 | TRK-233 | PR-E rebuild ESM dist regression（chunk-split eval order）|
| TD-034 | TRK-234 | （配對 TRK-233 codify S6 規則）|
| TD-035 | TRK-235 | `skipA11y: true` debt 藏起來，audit 顯示 13/17 多餘 |
| TD-036 | TRK-236 | pre-commit hook（Plan C）擋 `^const \w+\s*=\s*window\.__\w+\s*;` |
| TD-039 | TRK-239 | `check_dist_source_consistency.py` + `check_skip_a11y_justification.py` |

## 不在 mapping 範圍

- **`ADR-NNN`** — 架構決策 ID，永不重編號（ADR-020 namespace policy）
- **`S#NNN`** — sprint planning ledger，保留原 namespace（時序語意不同於 TRK）
- **`Trap #N`**（[`windows-mcp-playbook.md`](windows-mcp-playbook.md)）— 環境 trap catalogue，獨立 namespace
- **`pitfall #N`**（[`windows-mcp-playbook.md`](windows-mcp-playbook.md)）— 同上

## 引用慣例（v2.8.1+）

```
✅ 新 entry / commit / PR body / 文件： Resolves TRK-228
✅ 引用歷史記載: 依 source 原文保留（如 ADR-014 內的 TECH-DEBT-XXX 字樣若是歷史敘述則照原樣）
⚠️ 過渡期 PR body 寫 `Resolves TD-028` 仍可 work，CI 透過本 mapping 自動翻譯，但會 emit warning「用 TRK-228 取代」
```

## 影響的 lint / 工具

| 工具 | 狀態 |
|---|---|
| `scripts/tools/lint/check_techdebt_drift.py` | **phantom no-op**（`known-regressions.md` 已 phantom-delete）。chunk 4（issue [#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) III-2b）用新的 `check_planning_status_sync.py` 取代 |
| `scripts/dx/generate_planning_index.py` | chunk 2a 新增，掃 frontmatter 產 `planning-index.md` |
| Pre-commit hooks | 暫不擋舊 ID（過渡期）；chunk 5 收編後正式 deprecate |

## CHANGELOG-archive 與 docs/internal/archive 不動

[CHANGELOG-archive.md](../../CHANGELOG-archive.md) + [docs/internal/archive/](archive/) 的歷史敘述**不重寫**——pre-v2.2.0 引用作歷史保留，需要對應 TRK 時來查本表。

## 後續工作

本文件落地（chunk 1）之後：

- chunk 2a — `generate_planning_index.py`（產 `planning-index.md`）
- chunk 3 — 既有 backlog frontmatter migration（一律 TRK-NNN，後續 entries 從 TRK-300+ 分配）
- chunk 2b — `check_planning_status_sync.py` + CI wire（讀 PR body `Resolves TRK-NNN`，驗 frontmatter status）
- chunk 5 — CLAUDE.md 起手式收編 + dev-rules.md / commit-convention.md 強制 TRK

## 關聯

- [ADR-020](../adr/020-planning-ssot.md) — 本 mapping 的政策依據
- [issue #379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) — 本 mapping 是 chunk 1 deliverable
- [dev-rules.md §P1](dev-rules.md) — commit trailer 規範
- [dx-tooling-backlog.md](dx-tooling-backlog.md) — TRK-001~018 的 source
