---
title: "ADR-015: 全面改用 `[data-theme]` 單軌 dark mode，移除 Tailwind `dark:` 變體"
tags: [adr, design-tokens, dark-mode, v2.7.0]
audience: [frontend-developers, designers, maintainers]
version: v2.9.0
lang: zh
id: ADR-015
tracking_kind: adr
status: accepted
domain: portal
created_at: 2026-04-18
updated_at: 2026-11-24
---
# ADR-015: 全面改用 `[data-theme]` 單軌 dark mode，移除 Tailwind `dark:` 變體

> **Language / 語言：** **中文 (Current)** | [English](./015-data-theme-single-track-dark-mode.en.md)

## 狀態

✅ **Accepted**（v2.7.0，2026-04-16）

2026-05-13 修訂：JSX 原始碼搬到 `tools/portal/src/`，驗收的掃描路徑隨之更新。

2026-11-24 修訂：改寫為現行行為。決策不變。

## 決策

Portal 的 JSX 工具只有一套 dark mode 機制：`<html>` 上的 `data-theme` 屬性。深淺色的換色全部交給 `docs/assets/design-tokens.css` 裡的 token（CSS 變數）。**JSX 不寫 Tailwind 的 `dark:` 變體**（例如 `dark:bg-slate-900`）。

具體規則：

1. JSX 不使用 `dark:` 前綴。
2. 顏色寫成引用 token 的 Tailwind arbitrary value（方括號裡直接寫 CSS 值的 class，例如 `bg-[color:var(--da-color-surface)]`）；遷移舊元件時，原本的 `dark:xxx` 直接刪掉，不改寫成別的東西。
3. 切換主題就是改 `<html data-theme="…">`，元件本身不判斷目前是深色還是淺色。

### 範例：同一個 class，跟著 `data-theme` 換色

元件只寫一次背景色，引用 `--da-color-surface`：

```jsx {.quote src="tools/portal/src/interactive/tools/master-onboarding.jsx"}
<div className="border border-[color:var(--da-color-surface-border)] rounded-lg p-5 bg-[color:var(--da-color-surface)]">
```

這個 token 在 `design-tokens.css` 裡有淺色與深色兩個值，深色值掛在 `[data-theme="dark"]` 底下：

```css {.quote src="docs/assets/design-tokens.css"}
:root {
// ...
  --da-color-surface:         #ffffff;
// ...
[data-theme="dark"] {
// ...
  --da-color-surface:         #1e293b;
```

結果：`<html data-theme="light">` 時這張卡片的背景是 `#ffffff`，`<html data-theme="dark">` 時是 `#1e293b`。JSX 一個字都不用改，也不需要第二個 class。

主題由 `docs/assets/jsx-loader.html` 決定：使用者可在 Light／Dark／System 三者間切換，選擇存在 `localStorage` 的 `__da_theme_pref`，沒有存過就跟著作業系統的 `prefers-color-scheme`。

## 背景

在這個決策之前，portal JSX 同時有兩套 dark mode 機制：

1. **Tailwind `dark:` 變體**：靠 `<html>` 上有沒有 class `dark` 決定，例如 `<div class="bg-white dark:bg-slate-900">`。
2. **`design-tokens.css` 的 `[data-theme="dark"]`**：靠 `<html>` 上的屬性決定，token 自動換值。

兩套之間沒有橋接：切了 class 不會切屬性，反之亦然。實際造成的問題：

- 遷移到一半的元件，用 token 的節點與用 `dark:` 的節點配不上，例如白底 tooltip 配上深色文字。
- 每加一個顏色都要在 Tailwind class 與 token 兩邊各寫一次。
- dark mode 切換出錯時，很難判斷是哪一套造成的。

選單軌的理由：

1. `design-tokens.css` 是所有顏色的唯一定義處（Single Source of Truth）；若還有第二套並行的換色機制，token 就失去意義。
2. [ADR-014](014-wizard-arbitrary-value-token-migration.md) 決定把顏色都改寫成引用 token 的 arbitrary value；顏色進了 token 之後，`dark:` 只剩殘留。
3. 保留雙軌唯一的好處是「大家熟悉 Tailwind 的寫法」，比不上整體一致。

## 後果與已知限制

### 正面

- 有 token 的畫面，dark mode 行為只取決於 `data-theme` 一處，全畫面一致。
- 新元件的顏色只寫一次，不必為深色再寫一份。

### 已知限制

- **沒有 token 的顏色不會換色。** Tailwind 內建調色盤的 class（例如 `bg-red-500 text-white`）不經過 token，深色主題下仍是同一個顏色。這類 class 目前在 `tools/portal/src/` 的 JSX 裡仍大量存在，例如狀態色、步驟完成色。本決策只解決「有 token 的畫面上兩套機制並存」，不解決「顏色根本沒進 token」；後者另案追蹤。`design-token-usage` lint 只抓 hex 色碼、style 裡的 px 數值與錯誤／警告 token 當前景色，不抓這類調色盤 class。
- **loader 仍保留 class `dark`。** 決策原本要求 loader 只設 `data-theme`、拿掉 Tailwind 的 `darkMode` 設定；現行的 `jsx-loader.html` 沒有這樣做：它設定 `tailwind.config = { darkMode: 'class' }`，切到深色時同時加上 class `dark` 與 `data-theme="dark"`，因為 loader 自己的外框樣式（返回連結、主題按鈕、載入動畫）是用 `html.dark` 選擇器寫的。

  ```html {.quote src="docs/assets/jsx-loader.html"}
      document.documentElement.classList.toggle('dark', isDark);
      document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
  ```

  對 JSX 工具沒有影響，因為 JSX 裡沒有 `dark:` 變體。但這也代表：有人在 JSX 重新寫進 `dark:`，畫面上會照樣生效，不會露出問題。
- **「不准寫 `dark:`」沒有機制守。** 決策時預定補一支 pre-commit lint，至今沒有實作；目前靠 review。要確認現況，在 repo 根目錄跑：

  ```bash
  grep -rnE '\bdark:[a-z]' tools/portal/src --include='*.jsx'
  ```

  應無輸出。註解裡提到 `dark:`（例如記錄某元件已移除 `dark:`）不算違反，上面的樣式只比對 `dark:` 後面直接接 class 名稱的寫法。

## 考慮過的替代方案

| 方案 | 內容 | 結論 |
|---|---|---|
| 兩套同時生效 | Tailwind 設定 `darkMode: ['class', '[data-theme="dark"]']`，讓 `dark:` 也吃屬性 | 不採用：兩套仍並存，複雜度沒有下降 |
| loader 同步兩者 | loader 切主題時同時改 `<html class="dark">` 與 `data-theme="dark"` | 不採用：只是補丁，token 與 class 仍是兩個來源 |
| **只留 `[data-theme]`** | JSX 移除所有 `dark:`，換色交給 token | **採用**：只有一個來源 |

## 相關

- [ADR-014: Wizard Token Arbitrary-Value 遷移策略](014-wizard-arbitrary-value-token-migration.md) — 顏色如何改寫成 token；與本 ADR 搭配使用
- 實作位置：`docs/assets/design-tokens.css`（token 的淺色與深色值）、`docs/assets/jsx-loader.html`（主題切換）、`tools/portal/src/`（JSX 原始碼）
