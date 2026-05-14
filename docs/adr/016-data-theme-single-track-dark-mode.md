---
title: "ADR-016: 全面改用 `[data-theme]` 單軌 dark mode，移除 Tailwind `dark:` 變體"
tags: [adr, design-tokens, dark-mode, v2.7.0]
audience: [frontend-developers, designers, maintainers]
version: v2.8.0
lang: zh
id: ADR-016
tracking_kind: adr
status: accepted
domain: portal
created_at: 2026-04-18
updated_at: 2026-05-13
---
# ADR-016: 全面改用 `[data-theme]` 單軌 dark mode，移除 Tailwind `dark:` 變體

> **Language / 語言：** **中文 (Current)** | [English](./016-data-theme-single-track-dark-mode.en.md)

> 此決策是 v2.7.0 後續所有 token 遷移都仰賴的前提，獨立成 ADR。

## 狀態

✅ **Accepted**（v2.7.0, 2026-04-16）— 實作於 `deployment-wizard.jsx`
遷移（commit `8634ea2`），83 個 `dark:` variants 全數移除。後續
（rbac / cicd / threshold-heatmap）沿用同一規則。

## 背景

v2.6.x 之前，portal JSX 有兩套 dark mode 機制並存：

1. **Tailwind `dark:` 變體**（class-based）：`<div class="bg-white dark:bg-slate-900">`
2. **design-tokens.css `[data-theme="dark"]` 屬性**：token 自動換色
   `:root { --da-color-bg: #fff } [data-theme="dark"] { --da-color-bg: #0b1220 }`

兩套機制**沒有橋接**：切換 class 不會切 attribute、反之亦然。`deployment-wizard.jsx` critique
時確認這會造成：

- 半遷的元件：token-化的節點會跟 `dark:` 節點對配不上（白底 tooltip 配深色文字）
- 系統性維護負擔：每新增一個顏色都要在 Tailwind class 與 token 兩邊同步
- dark mode 切換 bug 的根因幾乎不可 bisect

## 決策驅動力

1. `[data-theme]` 是 design-tokens.css 的 SSOT；若要讓 token 有實質價值，
   不能有第二套並行系統
2. ADR-015（Option A arbitrary-value 改寫）把所有顏色吃進 token，`dark:` 就變成殘影
3. 維持雙軌的唯一理由是「既有 tailwind pattern 熟悉」— 價值低於系統一致性

## 決策

**選 Option C**：全面改用 `[data-theme="dark"]` attribute-based dark mode，
**移除所有 Tailwind `dark:` variants**。

實作要求：
1. 任何新 JSX 禁止使用 `dark:` 前綴（pre-commit lint 後補）
2. Token 遷移時，`dark:xxx` 直接刪除（換色由 token 負責）
3. `tailwind.config`（若啟用）移除 `darkMode` 配置
4. `jsx-loader` 切換主題：`document.documentElement.setAttribute('data-theme', 'dark')`
   （不 toggle class `dark`）

## 考慮過的替代方案

| 方案 | 內容 | 決策 |
|---|---|---|
| A | Tailwind config `darkMode: ['class', '[data-theme="dark"]']`：讓兩套同時生效 | ❌ 並行複雜度不降 |
| B | `jsx-loader` 同時 toggle `<html class="dark">` 和 `data-theme="dark"` | ❌ 補丁式，不解決 token 與 class 雙源 |
| **C** | 全面改 `[data-theme]`，移除 `dark:` | ✅ 選用，最乾淨 |

## 後果

### 正面
- **遷移後所有工具 dark mode 行為可預期**：切 `data-theme` 一處，全畫面一致
- 新加入的開發者不會誤用 `dark:`
- 後續 rbac / cicd / threshold-heatmap 遷移因此節省每個元件約 30–40% 改寫時間
  （不用雙寫顏色）

### 負面 / 風險
- **既有 tools 尚未遷移的部分會有 dark mode 斷層**：例如 config-lint 目前還留
  部分 `dark:`，切換主題時會呈現半套視覺 → 列為 v2.7.0 收束驗收項
- **後續發現**：runtime axe 掃 threshold-heatmap 時發現
  `bg-red-500 text-white` 這種 palette 硬編碼 **沒有 token 也就沒有 dark mode 換色**，
  本 ADR 幫不上（TRK-205）。即：本 ADR 解決了**有 token 的畫面的 dark 雙軌**，
  沒解決「palette 殘留根本不進 dark pipeline」的問題。
- 需要在 v2.7.0 收束時加 `grep 'dark:' docs/**/*.jsx` 驗收，否則 `dark:` 殘影
  會在後續 code review 被忽略

## 生效範圍

- v2.7.0 起，新 JSX 禁止 `dark:` 變體
- 既有 tools 在各自 token migration PR 清除 `dark:`
- v2.7.0 收尾前需通過 `grep -r 'dark:' docs/getting-started docs/interactive/tools` 為空

## 關聯

- Commit `8634ea2`（deployment-wizard 遷移，首次落地）
- [ADR-015: Wizard Token Arbitrary-Value 遷移策略](015-wizard-arbitrary-value-token-migration.md) — 兩者一起構成 v2.7.0 標準遷移套件
- TRK-205（palette 殘留導致 dark mode 斷層的案例）
