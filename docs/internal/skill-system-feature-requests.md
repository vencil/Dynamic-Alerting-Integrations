---
title: "Skill-system Feature Requests（upstream：Anthropic / Cowork）"
tags: [internal, dx, ai-agent, upstream]
audience: [maintainers, ai-agents]
version: v2.9.1
verified-at-version: v2.8.1
lang: zh
---

# Skill-system Feature Requests — upstream（Anthropic / Cowork）（TRK-309）

> **用途**：收集**不是 Vibe 能單方解決**、需上游（Claude Code skill spec / Cowork plugin host）支援的 skill-system 改善。集中放在內部 SSOT，等對方 issue tracker / RFC 開放時，再從這份 doc PR 過去。**不入 v2.9.0 milestone**（非 Vibe deliverable），但 maintainer 該知道這些 gap。
>
> 來源：2026-05-21 session 對 superpowers plugin + 既有 skill 體系的評估（官方 skill 設置方式 / token 效率）。Vibe 內部能做的部分已落地（epic #570：plugin prune 建議、skill 優先級宣告、hook-vs-skill 矩陣、季度 audit）；本表是**剩下需上游做的**。

## 量化背景

當前 session 的 system prompt 約載入 80 個 skill 描述 ≈ **3500–4500 token**，每一輪都流入，與實際工作無關。100 輪 session ≈ 1M token 只為「知道有哪些 skill」。下列 FR 多數圍繞此 routing 開銷。

## FR 清單

### FR-01 — Project-scoped skill allowlist / disable

- **問題**：plugin 是 atomic install/uninstall，無法挑 subset；專案載入永遠用不到的 namespace。
- **量化影響**：Vibe（SRE/platform 專案）永不用 `marketing:*`（7）/ `productivity:*` / `design:*` 大半 / `algorithmic-art` / `web-artifacts-builder` ≈ 18 skill × ~50 token ≈ **~900–1000 token/turn**（~90k/session）純浪費。
- **提議**：`.claude/skill-disable.yaml` 或 plugin manifest 內 `disable: [skill1, ...]`，專案層宣告不載入。
- **上游 issue**：（未開）

### FR-02 — Keyword-gated lazy description loading（router tiering）

- **問題**：所有 skill 描述一次性全載，不論當前任務是否相關。
- **量化影響**：~60% 描述開銷可省（只在 prompt 出現對應 namespace keyword 時才展開該 namespace 描述）。
- **提議**：Tier-A always-on（高頻 + 結構性，如 vibe-*）；Tier-B keyword-gated（UserPromptSubmit hook 偵測 keyword → 動態 inject）。
- **上游 issue**：（未開）

### FR-03 — Skill description style guide + lint

- **問題**：描述格式高度不一致——有一句話模糊型（`web-artifacts-builder`）、Triggers 列舉型（`engineering:debug`，好範本）、多模式內嵌型（`data:data-context-extractor` 把 8 行 mode 文檔塞描述）、規範宣告型（`update-config` 把 mental model 塞描述）。膨脹 + false-positive。
- **提議**：style guide（強制 50–150 字 / Triggers 列首 / mode 與規範移至 body / `Skip when:` 反向條件）+ `skill-creator` 加 lint。
- **上游 issue**：（未開）

### FR-04 — Anti-trigger / supersedes / chains_to metadata 標準化

- **問題**：`claude-api` 是**唯一**有 `SKIP:` 反向條件的描述（設計優秀但未推廣）；無標準化的 supersede / chain 宣告。
- **Vibe 案例**：`vibe-workflow` vs `engineering:debug`、`vibe-dev-rules` vs `engineering:code-review` 的觸發競爭，目前靠 CLAUDE.md §Skill 優先級宣告（TRK-301）人工仲裁。
- **提議**：metadata 欄位 `skip_if: [...]` / `supersedes: [...]` / `chains_to: [...]`，由 host 解析。
- **上游 issue**：（未開）

### FR-05 — SKILL.md section anchor 部分載入

- **問題**：Skill tool 一次讀整份 SKILL.md。`consolidate-memory` / `engineering:code-review` 等 200+ 行，常只需其中一段。
- **提議**：`Skill(skill: "foo", section: "#bar")` 部分載入；SKILL.md 用標準 markdown anchor。
- **上游 issue**：（未開）

### FR-06 — Skill usage telemetry CLI

- **問題**：無法得知過去 N 個 session 用過哪些 skill → 沒證據刪死 skill。
- **Vibe 案例**：TRK-301（決定拔哪些 plugin）與 TRK-307（季度 audit）都需要這個數據才能客觀決策。
- **提議**：`claude skill-usage --since 30d`；死 skill 自動標建議 disable。
- **上游 issue**：（未開）

## 重新評估觸發

被動（事件驅動）：

- Anthropic / Cowork 釋出 skill-spec 改版時（對照本表哪些已被官方解決）
- Vibe baseline token 突然增加（暗示 skill ecosystem 改了）
- 新增 ≥5 個內部 / 外部 skill 時（routing 開銷再評估）

主動（recurring，避免本表爛成「許願墓地」）：

- **綁定 TRK-307 季度 audit**：每季跑 `make audit-rules` 時，巡檢本表 FR-01~06 — 上游是否已**靜默**解決任一項（spec 改版常不大肆宣傳，被動 trigger 抓不到）。SOP 步驟見 [`quarterly-audit-sop.md` §附帶巡檢](quarterly-audit-sop.md)。

## 附錄：糾錯（觀察到的具體描述缺陷）

非 FR、屬「上游該修描述」的具體案例：

- `init` / `review` / `security-review` 命名太通用，與 `engineering:code-review` 互打
- `data:data-context-extractor` 描述塞 8 行 mode 文檔（mode 該入 body）
- `engineering:*` 與 `operations:*` 大量功能重疊（incident-response / risk-assessment / change-request 同觸發詞）
- `loop` vs `schedule` 差異（dynamic vs cron）藏在描述細節
- `update-config` 把「為什麼 memory 不能取代 hook」mental model 塞描述（該入 body 首段）

## 附錄：中長期發想（非 FR，願景級）

- **F1 router 從「全載描述」改為「MCP 查詢」**：抽掉 system-prompt 全載，改 `mcp__skills__search(query)`；省 ~4000 token/turn baseline，付每次需要時一次 tool-call 往返。划算條件：每 session skill 觸發 < 8 次（Vibe 通常 2–4）。
- **F2 project skill addendum**：`.claude/skill-overrides/<skill>.md` 拼接專案專屬條目，不 fork 整份。
- **F3 skill 互鏈宣告（chains_to）**：`engineering:debug` → 自動 suggest `engineering:incident-response`。
- **F4 description precision telemetry**：記錄「描述出現 + keyword 命中 → 是否真呼叫」，產精準度報表給作者改寫。

## 關聯

- owner 矩陣：[`hook-vs-skill-coverage.md`](hook-vs-skill-coverage.md)（Vibe 內部能做的部分）
- epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-309
- 來源 session：2026-05-21 superpowers / skill-system 評估
