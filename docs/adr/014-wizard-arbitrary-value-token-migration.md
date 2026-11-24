---
title: "ADR-014: wizard.jsx design token 遷移採 Option A（Tailwind arbitrary value 全改寫）"
tags: [adr, design-tokens, v2.7.0]
audience: [frontend-developers, maintainers]
version: v2.9.0
lang: zh
id: ADR-014
tracking_kind: adr
status: accepted
domain: portal
created_at: 2026-04-18
updated_at: 2026-11-24
---
# ADR-014: wizard.jsx design token 遷移採 Option A（Tailwind arbitrary value 全改寫）

> **Language / 語言：** **中文 (Current)** | [English](./014-wizard-arbitrary-value-token-migration.en.md)

## 狀態

✅ **Accepted**（v2.7.0，2026-04-16）。

2026-11-24 修訂：改寫為現行行為。決策不變。

## 決策

**Portal 的 JSX 工具遷到 design token 時，整支工具一次改完：每個顏色類別都改寫成 Tailwind 的 arbitrary value（方括號裡直接寫 CSS 值的類別），值指向 `--da-color-*` token。** 寫法是 `bg-[color:var(--da-color-*)]`、`text-[color:var(--da-color-*)]`、`border-[color:var(--da-color-*)]`，`ring-` 等其他色彩類別同理。這就是標題的 Option A（三個選項見「考慮過的替代方案」）。

- 不用寫死的 Tailwind 色票類別（`bg-blue-600`、`text-slate-700` 這類），也不用 Tailwind 的 `dark:` 變體。深色主題由 token 在 `[data-theme="dark"]` 下換值處理，見 [ADR-015](015-data-theme-single-track-dark-mode.md)。
- 只遷關鍵元素、其他留著色票的做法不採用：每支工具都是整支改寫。
- 一時找不到對應語意 token 的狀態色（例如「已完成」的綠、「優先」的琥珀）可以暫留 Tailwind 類別，但要在 JSX 註解寫明原因，作為之後補 token 時的待辦清單。

`tools/portal/src/getting-started/wizard.jsx`（入門精靈）是第一支照這個做法改完的工具，也是後續工具的範本。它現在不含任何寫死的 Tailwind 色票類別；當初暫留的狀態色已改用語意 token（`--da-color-success`、`--da-color-warning`、`--da-color-semantic-other` 等）。

### 範例：步驟指示圓點

改寫前，「目前這一步」的圓點寫成 `bg-blue-600`，顏色固定是 `#2563eb`，深色主題下也不會變。改寫後，wizard.jsx 的步驟指示是這樣：

```jsx {.quote src="tools/portal/src/getting-started/wizard.jsx"}
        className={`flex-shrink-0 flex items-center justify-center w-10 h-10 rounded-full font-bold transition-all ${
          i < step
            ? "bg-[color:var(--da-color-accent)] text-[color:var(--da-color-accent-fg)]"
            : i === step
              ? "bg-[color:var(--da-color-accent)] text-[color:var(--da-color-accent-fg)] ring-4 ring-[color:var(--da-color-focus-ring)]"
              : "bg-[color:var(--da-color-surface-border)] text-[color:var(--da-color-muted)]"
```

`--da-color-accent` 在 `docs/assets/design-tokens.css` 依主題各有一個值：

```css {.quote src="docs/assets/design-tokens.css"}
:root {
// ...
  --da-color-accent:          #2563eb;
// ...
[data-theme="dark"] {
// ...
  --da-color-accent:          #60a5fa;
```

結果：淺色主題下圓點仍是 `#2563eb`，與改寫前相同；頁面切到 `[data-theme="dark"]` 時圓點自動變成 `#60a5fa`，JSX 不用為深色主題多寫任何類別。

## 背景

wizard.jsx 是 portal 的入門精靈，是新使用者最先接觸的工具。遷移前它完全沒用 `--da-color-*` token，顏色全是寫死的 Tailwind 色票（slate／blue／emerald／amber／red），所以深色主題下顏色不會跟著換。

要讓它跟上主題，有兩種機制：替每個顏色類別再加一個 `dark:` 變體（每個類別寫兩次），或讓類別直接吃會隨主題換值的 token。[ADR-015](015-data-theme-single-track-dark-mode.md) 選了只靠 `[data-theme]` 切換主題，`dark:` 這條路就不需要了。

剩下的問題是遷移範圍。當時有三個選項：

- **A**：所有顏色類別改寫成指向 token 的 arbitrary value。保持 Tailwind 寫法，但實際吃 token。
- **B**：只遷關鍵元素（角色卡、選項卡、主要按鈕、進度條、名詞提示），其他留著色票。
- **C**：延到下一版入門流程整體重寫時再處理。

wizard 是入門的門面，拿它當「新寫法」的示範價值最高，所以選了範圍最完整的 A。

## 後果與已知限制

- **className 變長。** 每個顏色類別從 `bg-blue-600` 變成 `bg-[color:var(--da-color-accent)]`，可讀性下降。
- **token 定義有缺陷時，影響面跟著放大。** 所有工具都吃同一組 token，一對對比不足的 token 會同時出現在每個用到它的地方。實例：`--da-color-tag-bg` 底配 `--da-color-muted` 字曾經對比不足，每個用到這一對的地方都一起不及格；這是 token 定義層的問題，現在的定義在淺色與深色主題下都已達 WCAG AA，但它示範了風險的形狀。修 token 的好處也一樣放大：改一處，所有工具一起修好。
- **沒有針對 token 配對的對比驗收。** 目前對比是逐支工具在畫面上量：`tests/e2e/wizard.spec.ts` 以 axe-core（自動無障礙檢查）跑 WCAG 2 A／AA 規則，任何違規都會讓測試失敗。但它只量精靈的第一步、預設主題；後面的步驟與深色主題沒有量到。另外 `design-token-usage` lint 會擋新增的「把飽和的 `--da-color-error`／`--da-color-warning` 當文字色」寫法。
- **機械強制只涵蓋一部分。** pre-commit 的 `design-token-usage`（只看本次 diff 新增的行）會擋新增的寫死 hex 色碼與新增的 `slate-*` 類別（規則見 [dev-rules S1](../internal/dev-rules.md)）；其他色票（`blue-*`、`emerald-*`、`amber-*`、`red-*`、`gray-*` 等）沒有 lint 擋，靠 review。仍有不少早期工具含寫死的色票類別，屬於尚未遷移的存量。
- **拼錯的 token 名不會被攔。** `var(--da-color-…)` 指向不存在的 token 時，那個顏色在畫面上就是沒有套用，不會報錯。檢查未定義 token 的 `undefined-tokens-check` 目前只掃 `docs/` 底下的檔案，而 JSX 原始碼在 `tools/portal/src/`，所以 JSX 裡的拼錯不在它的範圍內。

## 考慮過的替代方案

| 方案 | 優點 | 缺點 | 結論 |
|---|---|---|---|
| A：全部改寫成指向 token 的 arbitrary value | 深色主題只靠 token 一條路；整支工具都吃 token；可當其他工具的範本 | 初期改寫量大；className 變長 | ✅ 採用 |
| B：只遷關鍵元素 | 快 | 一半還是寫死的色票：看起來已經遷完，深色主題卻仍然壞，對比差異也難追，品質負債更隱蔽 | ❌ |
| C：延到下一版重寫 | 現在不用做 | 入門核心工作整個延版；後續工具失去範本 | ❌ |

## 相關

- `tools/portal/src/getting-started/wizard.jsx` — 第一支照本 ADR 改完的工具
- `docs/assets/design-tokens.css` — `--da-color-*` token 定義（淺色 `:root`、深色 `[data-theme="dark"]`）
- [ADR-015: `[data-theme]` 單軌 Dark Mode](015-data-theme-single-track-dark-mode.md) — 配對的深色主題決策
- [dev-rules S1](../internal/dev-rules.md) — 中性色禁用 `slate-*` 類別與其例外
