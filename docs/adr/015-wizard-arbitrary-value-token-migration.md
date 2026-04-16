---
title: "ADR-015: wizard.jsx design token 遷移採 Option A（Tailwind arbitrary value 全改寫）"
tags: [adr, design-tokens, phase-a0, v2.7.0]
audience: [frontend-developers, maintainers]
version: v2.7.0
lang: zh
---

# ADR-015: wizard.jsx design token 遷移採 Option A（Tailwind arbitrary value 全改寫）

> Originally recorded as **DEC-A** in `docs/internal/v2.7.0-planning.md §15`
> (Day 2, 2026-04-16 AM). Back-filled during Day 5 retrospective review —
> DEC-A shaped the token-migration pattern used by every subsequent Phase .a0
> batch (including Day 4 batch 4), so it deserved an ADR from day one.

## 狀態

✅ **Accepted**（v2.7.0 Day 2, 2026-04-16）— 實作見 commit `ec07914`
`refactor(jsx): migrate wizard.jsx core palette to design tokens (Phase .a0 DEC-A)`；
落地 69 occurrences 改寫，19 state-specific colors 明示保留。

## 背景

wizard.jsx（`docs/getting-started/wizard.jsx`，當時 ~900 LOC）是 Day 1 critique
發現的「最大 design system 脫鉤案例」：**0% token 採用**、100% Tailwind
core palette（slate/blue/emerald/amber/red）硬編碼、是 portal flagship onboarding
工具但完全不吃 `--da-color-*` token。

Phase .a0 必須把它吃進 token 系統，但三種遷移路徑互有取捨：

- **A**. 全面 Tailwind → `bg-[color:var(--da-color-*)]` arbitrary value 改寫
       → **保持 Tailwind 寫法，但實際吃 token**。
- **B**. 關鍵元素（Role card / Option card / Primary button / Progress bar / GlossaryTip）
       遷移，其他保留 Tailwind core palette。
- **C**. 延到 v2.8.0 Master Onboarding 重寫時一起處理。

## 決策驅動力

1. wizard 是 flagship，**拿它當「新寫法」示範**的價值最高
2. `var(--da-color-*)` 的真正威力是 `[data-theme="dark"]` 時 token 自動換色，
   Tailwind `dark:` 變體需要 double up 每個類別 → Option A 在 DEC-F 選定
   `[data-theme]` 單軌後自然勝出
3. Option B 留一半 Tailwind 的結果是「看起來已經遷但 dark mode 依舊壞」→
   品質負債反而更隱蔽
4. Option C 把整個 onboarding 核心工作都延版，風險不可接受

## 決策

**採 Option A**：全面 `bg-[color:var(--da-color-*)]` / `text-[color:var(--da-color-*)]` /
`border-[color:var(--da-color-*)]` arbitrary value 改寫。明確允許保留的
**19 個 state-specific colors**（如 `bg-blue-600` 於 active selection）需在
註解標註原因，作為未來審查的 waiver list。

## 考慮過的替代方案

| 方案 | 優 | 劣 | 結論 |
|---|---|---|---|
| A | dark mode 單軌；全 token 化；可抄成其他 wizard 範本 | 初期改寫量大；className 變長 | ✅ 選用 |
| B | 快 | 留半 Tailwind → dark mode / contrast 差異難追蹤 | ❌ |
| C | 零即時工作 | wizard flagship 與後續批次失去模板；延版風險 | ❌ |

## 後果

### 正面
- **設立 Phase .a0 標準 migration pattern**：Day 3 deployment-wizard、Day 4 batch 4
  （rbac / cicd / threshold-heatmap）都直接沿用 Option A 的改寫風格與 waiver 註解
- dark mode 與 DEC-F（`[data-theme]` 單軌）乾淨對齊
- 新加入的開發者只要看 wizard.jsx diff 就知道怎麼遷下一個工具

### 負面 / 風險
- className 字串平均長 ~2x → 可讀性下降
- arbitrary value 的 CSS 最終輸出未經 Tailwind tree-shake 整合，**bundle size 增約 2–4%**
  （v2.7.0 暫不優化；Phase .e 評估是否引入 postcss preset）
- **Day 5 runtime axe-core 發現的 `--da-color-tag-bg` + `--da-color-muted` 對比不足
  不是 Option A 造成的 → 是 token 定義層問題（TECH-DEBT-003），但 Option A
  放大了影響面（每個用到這對 token 的 step indicator 都會吃到同一個 bug）**
- **Retrospective 教訓**：Option A 讓「token 定義層的缺陷」一次被多個工具吸收，
  表面看是「一次改完整個畫面」的好處，實務是「token 本身不達 AA 時，
  系統性破壞面也放大」。下版應同步跑 runtime contrast audit 作為 token 定義驗收。

## 生效範圍

- v2.7.0 Phase .a0 所有 JSX token 遷移一律採 Option A 寫法（不允許 B）
- waiver list 寫在 PR description；超過 20 個 waiver 視為「改寫不徹底」，需要 review

## 關聯

- `docs/getting-started/wizard.jsx`（實作）
- Commit `ec07914`（Day 2 landed）
- DEC-F（ADR-016，配對的 dark mode 單軌決策）
- DEC-G（`docs/internal/dev-rules.md` §S1，配對的 gray 中性色 style rule）
- TECH-DEBT-003（Day 5 發現的 token-pair contrast 問題，非本 ADR 直接造成）
