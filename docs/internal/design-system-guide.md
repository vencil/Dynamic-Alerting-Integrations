---
title: "Design System Guide"
tags: [documentation, internal, design]
audience: [maintainers]
version: v2.6.0
lang: zh
---

# 設計系統指南 v2.6.0

## 1. 設計系統概覽

### 問題背景
v2.5.0 存在三套平行的 CSS 系統，導致色彩、間距、字型等 design token 分散在多個檔案中：
- index.html（Tools Hub）的內嵌 CSS
- jsx-loader.html（JSX 工具載入器）的內嵌 CSS
- 各個 JSX 工具的 inline 樣式

此架構造成維護困難、一致性低、重複定義等問題。

### v2.6.0 解決方案
建立統一的 SSOT（Single Source of Truth）：**`docs/assets/design-tokens.css`**

兩個消費端均透過 `<link rel="stylesheet">` 載入此檔案：
- **index.html** (Tools Hub)：`<link href="../assets/design-tokens.css" rel="stylesheet" />`
- **jsx-loader.html** (JSX 工具載入器)：`<link href="../assets/design-tokens.css" rel="stylesheet" />`

所有 JSX 工具直接使用 CSS Custom Properties（var()），無需感知載入機制。

---

## 2. Token 命名規範

### 命名模式
```
--da-{category}-{element}-{modifier}
```

### 命名要素說明

| 位置 | 說明 | 範例 |
|------|------|------|
| `da` | Prefix，代表 Dynamic Alerting | 所有 token 必備 |
| `{category}` | token 類別（見下表） | color, space, font, shadow 等 |
| `{element}` | 應用對象或邏輯分組 | primary, secondary, muted, 或 text, border, bg 等 |
| `{modifier}` | 可選，進一步細化（如深淺變化或狀態） | hover, disabled, light, dark 等 |

### 支援的 Categories

| Category | 用途 | 命名示例 |
|----------|------|---------|
| `color` | 色彩（語義色、icon 類別色、operational mode 色等） | `--da-color-primary`, `--da-color-success-light` |
| `space` | 間距（基於 8px grid） | `--da-space-xs`, `--da-space-md-lg` |
| `font` | 字型（family、size、weight、line-height） | `--da-font-family-sans`, `--da-font-size-lg` |
| `shadow` | 陰影 | `--da-shadow-subtle`, `--da-shadow-modal` |
| `radius` | 圓角半徑 | `--da-radius-md`, `--da-radius-full` |
| `transition` | 過渡動畫時間 | `--da-transition-fast`, `--da-transition-slow` |

### 命名原則
1. **簡潔清晰**：避免過長的名稱，但要能自解釋
2. **分層結構**：从全局 → 分類 → 具體對象，逐層細化
3. **狀態修飾**：hover、disabled、focus 等狀態放在名稱末尾
4. **無內容涵義**：不使用 "blue", "red" 等顏色名稱；改用 "primary", "success", "error" 等語義名稱
5. **深淺變化**：統一用 "light" / "dark" 後綴表示深淺，不用 "10", "100" 等數字

---

## 3. 色彩系統

### 3.1 Light / Dark 主題色彩

整個設計系統支援淺色（Light）及深色（Dark）主題，每個色彩 token 在兩套主題中均有定義。

#### 主要色彩家族

**Primary（主色）**
- `--da-color-primary`：主按鈕、強調文字、active tab
- `--da-color-primary-light`：主色淺色版本（Dark 模式中用於低對比度背景）
- `--da-color-primary-dark`：主色深色版本（Light 模式中用於高對比度前景）

**Secondary（次色）**
- `--da-color-secondary`：次級按鈕、輔助元素
- `--da-color-secondary-light` / `--da-color-secondary-dark`

**Muted（中性色）**
- `--da-color-muted`：禁用狀態、placeholder 文字
- `--da-color-muted-light` / `--da-color-muted-dark`

**Background 與 Foreground**
- `--da-color-bg`：頁面背景色
- `--da-color-bg-elevated`：卡片、dropdown 背景色
- `--da-color-fg`：正文前景色（文字）
- `--da-color-fg-muted`：次級文字（如 label、hint）
- `--da-color-border`：邊框色
- `--da-color-border-subtle`：細線邊框色

### 3.2 語義色（Semantic Colors）

用於傳達消息、狀態、反饋：

| Token | Light 模式 | Dark 模式 | 使用場景 |
|-------|-----------|---------|---------|
| `--da-color-success` | #10b981 | #34d399 | 成功、通過、綠燈 |
| `--da-color-success-light` | #d1fae5 | #065f46 | Success 背景色 |
| `--da-color-warning` | #f59e0b | #fbbf24 | 警告、注意、黃燈 |
| `--da-color-warning-light` | #fef3c7 | #78350f | Warning 背景色 |
| `--da-color-error` | #ef4444 | #f87171 | 錯誤、危險、紅燈 |
| `--da-color-error-light` | #fee2e2 | #7f1d1d | Error 背景色 |
| `--da-color-info` | #3b82f6 | #60a5fa | 資訊提示、藍燈 |
| `--da-color-info-light` | #dbeafe | #1e3a8a | Info 背景色 |

### 3.3 Icon 類別色

為不同功能類別的 Icon 預定義色彩：

| Token | Light 模式 | Dark 模式 | 類別 |
|-------|-----------|---------|------|
| `--da-color-icon-validation` | #059669 | #10b981 | 驗證、validation |
| `--da-color-icon-cli` | #2563eb | #3b82f6 | CLI 工具、terminal |
| `--da-color-icon-rules` | #7c3aed | #a78bfa | Rule Pack、告警規則 |
| `--da-color-icon-wizard` | #dc2626 | #ef4444 | Wizard、引導工具 |
| `--da-color-icon-dashboard` | #0891b2 | #06b6d4 | Dashboard、儀表板 |
| `--da-color-icon-chart` | #d97706 | #f59e0b | Chart、圖表分析 |

### 3.4 Journey Phase 色

標記告警解決旅程中的不同階段：

| Token | 顏色 | 對應階段 |
|-------|------|---------|
| `--da-color-phase-deploy` | 紫色 | Deploy、部署階段 |
| `--da-color-phase-configure` | 青色 | Configure、配置階段 |
| `--da-color-phase-monitor` | 藍色 | Monitor、監控階段 |
| `--da-color-phase-troubleshoot` | 橙色 | Troubleshoot、排故階段 |
| `--da-color-phase-reference` | 灰色 | Reference、參考文件 |

### 3.5 Operational Mode 色

表示告警的三態運營模式：

| Token | Light 模式 | Dark 模式 | 對應模式 |
|-------|-----------|---------|---------|
| `--da-color-mode-normal` | #10b981 | #34d399 | Normal（正常運營） |
| `--da-color-mode-silent` | #6b7280 | #9ca3af | Silent（靜默模式） |
| `--da-color-mode-maintenance` | #f59e0b | #fbbf24 | Maintenance（維護模式） |

---

## 4. [data-theme] 切換機制

### 4.1 Attribute vs Class

本設計系統使用 **`[data-theme]` attribute**（而非 class），理由：
- 避免與 Tailwind、Bootstrap 等框架的 `class="dark"` 衝突
- 更清晰地表達「這是一個聲明式的系統狀態」，而非「樣式修飾符」
- CSS selector 的特異性更明確

### 4.2 三態主題切換

用戶可選擇三種主題偏好：

```javascript
// Light 模式
document.documentElement.dataset.theme = 'light';

// Dark 模式
document.documentElement.dataset.theme = 'dark';

// System（跟隨系統設定）
document.documentElement.dataset.theme = 'system';
```

當設為 `system` 時，JavaScript 應監聽 `prefers-color-scheme` 媒體查詢，並動態切換 CSS 變數：

```javascript
const systemTheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
// 應用對應的 CSS 變數集合
```

### 4.3 狀態持久化

用戶選擇的主題偏好應存儲在 localStorage，鍵名為 `__da_theme_pref`：

```javascript
// 保存用戶選擇
localStorage.setItem('__da_theme_pref', 'dark');

// 初始化時讀取
const savedTheme = localStorage.getItem('__da_theme_pref') || 'system';
document.documentElement.dataset.theme = savedTheme;
```

### 4.4 Fallback 機制

若 localStorage 不可用（如隱私瀏覽模式），應 fallback 至內存變數：

```javascript
let themePreference = 'system'; // 內存變數
try {
  themePreference = localStorage.getItem('__da_theme_pref') || 'system';
} catch (e) {
  // localStorage 不可用，保持內存變數
}
document.documentElement.dataset.theme = themePreference;
```

### 4.5 初始化時序

為避免頁面載入時出現色彩閃爍（Flash of Unstyled Content），主題切換的初始化代碼**必須放在 `<head>` 中**，並在 CSS 和其他 JavaScript 載入前執行：

```html
<!DOCTYPE html>
<html>
<head>
  <script>
    // 提前初始化主題，避免 FOUC
    (function() {
      const saved = localStorage.getItem('__da_theme_pref') || 'system';
      document.documentElement.dataset.theme = saved;
      if (saved === 'system') {
        const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        document.documentElement.dataset.theme = dark ? 'dark' : 'light';
      }
    })();
  </script>
  <link rel="stylesheet" href="../assets/design-tokens.css" />
  <!-- 其他 CSS -->
</head>
<body>
  <!-- 頁面內容 -->
</body>
</html>
```

---

## 5. 間距系統 (8px Baseline Grid)

整個設計系統基於 **8px baseline grid**，確保所有間距都能對齐至 8 的倍數。

### 間距 Token 列表

| Token | 像素值 | 使用場景 |
|-------|--------|---------|
| `--da-space-xs` | 4px | Icon 內間距、極小元素間隙 |
| `--da-space-sm` | 8px | 按鈕內間距（水平）、緊密間隔 |
| `--da-space-md` | 16px | 卡片內間距、標準間隔 |
| `--da-space-lg` | 24px | section 間隔、寬鬆間隔 |
| `--da-space-xl` | 32px | 頁面邊距、大間隔 |
| `--da-space-2xl` | 48px | hero section、大幅度間隔 |
| `--da-space-3xl` | 64px | 頁面頂層間隔、超大間隔 |

### 複合間距

某些場景需要 vertical + horizontal 組合：

```css
/* 範例：卡片標準內間距 */
padding: var(--da-space-md) var(--da-space-lg);
/* 相當於 16px (上下) + 24px (左右) */
```

---

## 6. Typography（文字系統）

### 6.1 字型家族

| Token | 設定值 | 用途 |
|-------|--------|------|
| `--da-font-family-sans` | `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif` | 正文、標題、UI 文字 |
| `--da-font-family-mono` | `"SF Mono", Monaco, "Cascadia Code", "Roboto Mono", Menlo, Courier, monospace` | 代碼、CLI 命令、metric 顯示 |

### 6.2 字型大小（7 階）

| Token | 像素值 | 用途 |
|-------|--------|------|
| `--da-font-size-xs` | 12px | 輔助文字、badge、細小標籤 |
| `--da-font-size-sm` | 14px | label、hint、次級文字 |
| `--da-font-size-md` | 16px | 正文、多數 UI 文字（**默認值**） |
| `--da-font-size-lg` | 18px | 卡片標題、強調文字 |
| `--da-font-size-xl` | 20px | section 標題（`<h3>` 級） |
| `--da-font-size-2xl` | 24px | 頁面標題（`<h2>` 級） |
| `--da-font-size-3xl` | 32px | Hero 標題（`<h1>` 級） |

### 6.3 字型權重

| Token | 值 | 使用場景 |
|-------|-----|---------|
| `--da-font-weight-regular` | 400 | 正文、預設文字 |
| `--da-font-weight-medium` | 500 | label、較強調的文字 |
| `--da-font-weight-semibold` | 600 | 小標題、強調 |
| `--da-font-weight-bold` | 700 | 主標題、重點突出 |

### 6.4 行高

| Token | 值 | 備註 |
|-------|-----|------|
| `--da-line-height-tight` | 1.2 | 標題、緊密排版 |
| `--da-line-height-normal` | 1.5 | 正文、多數情況 |
| `--da-line-height-relaxed` | 1.75 | 長文本、易讀性優先 |

### 6.5 預設字型樣式

所有 JSX 工具應在根元素套用：
```css
font-family: var(--da-font-family-sans);
font-size: var(--da-font-size-md);
font-weight: var(--da-font-weight-regular);
line-height: var(--da-line-height-normal);
color: var(--da-color-fg);
```

---

## 7. Shadow（陰影）/ Radius（圓角）/ Transition（過渡）

### 7.1 陰影系統（3 層）

| Token | CSS 值 | 使用場景 |
|-------|--------|---------|
| `--da-shadow-subtle` | `0 1px 2px rgba(0, 0, 0, 0.05)` | 細微陰影、hover 狀態 |
| `--da-shadow-hover` | `0 4px 6px rgba(0, 0, 0, 0.1), 0 1px 3px rgba(0, 0, 0, 0.08)` | 按鈕 hover、卡片浮起 |
| `--da-shadow-modal` | `0 20px 25px rgba(0, 0, 0, 0.15), 0 10px 10px rgba(0, 0, 0, 0.12)` | Modal、dropdown、overlay |

### 7.2 圓角系統（5 等）

| Token | 像素值 | 使用場景 |
|-------|--------|---------|
| `--da-radius-sm` | 2px | 細微邊框、icon 邊角 |
| `--da-radius-md` | 6px | 按鈕、輸入框、卡片（**常用**） |
| `--da-radius-icon` | 8px | Icon 容器、badge |
| `--da-radius-lg` | 12px | 大卡片、modal |
| `--da-radius-pill` | 9999px | 膠囊形、全圓形按鈕 |
| `--da-radius-full` | 50% | 完全圓形（如 avatar） |

### 7.3 過渡時間（3 速度）

| Token | 毫秒 | 使用場景 |
|-------|------|---------|
| `--da-transition-fast` | 150ms | 快速回應、icon 變化 |
| `--da-transition-base` | 300ms | 標準過渡（**常用**）、顏色變化 |
| `--da-transition-slow` | 500ms | Modal 出入、大幅動畫 |

---

## 8. 全局 Utilities

### 8.1 Focus-visible（統一焦點指示）

所有互動元素（`button`, `a`, `input`, `select` 等）的 `:focus-visible` 樣式已在 `design-tokens.css` 中全局定義，無需各檔案重複實作：

```css
/* design-tokens.css 中的全局定義 */
*:focus-visible {
  outline: 2px solid var(--da-color-primary);
  outline-offset: 2px;
}
```

新工具**不需要**在自己的 CSS 中重複定義焦點樣式。

### 8.2 Skip Link（`.da-skip-link`）

為輔助技術提供快速導航鏈接，應在每個 HTML 頁面的 `<body>` 最開始放置：

```html
<a href="#main-content" class="da-skip-link">Skip to main content</a>
<main id="main-content">
  <!-- 主要內容 -->
</main>
```

`.da-skip-link` 預設隱藏，但在焦點時出現（`:focus-visible` 狀態下可見）。

### 8.3 主題切換按鈕（`.da-theme-toggle`）

推薦在 Hub 或工具頁面放置主題切換控件：

```html
<button class="da-theme-toggle" aria-label="Toggle dark mode">
  <!-- 日/月 icon，根據當前主題顯示 -->
</button>
```

此按鈕應綁定以下邏輯：
```javascript
const button = document.querySelector('.da-theme-toggle');
button.addEventListener('click', () => {
  const current = document.documentElement.dataset.theme;
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('__da_theme_pref', next);
});
```

### 8.4 Toast 通知（`.da-toast`）

用於短暫通知消息，支援多種變體（success, warning, error, info）：

```html
<div class="da-toast da-toast-success">
  操作成功！
</div>
```

CSS 中應包含：
- 自動淡出動畫（3-4 秒後消失）
- 固定位置（右下角或頂部中心）
- 適當的內間距和圓角
- 語義色背景（根據 `da-toast-*` 變體）

---

## 9. 新工具開發 Checklist

開發新的互動工具時，務必遵循以下清單：

### ✅ 必做項目

- [ ] **使用 `var(--da-*)`**：所有色彩、間距、字型、陰影均使用 CSS Custom Property，禁止 hardcoded hex colors（如 `#3b82f6`）、pixel 值（如 `margin: 16px`）等

- [ ] **JSX 檔案中無 CSS import**：`jsx-loader.html` 已自動載入 `design-tokens.css`，工具內的 `<style>` 標籤應僅參考全局 token，勿重複定義

- [ ] **焦點樣式自動支援**：無需在各工具中實作 `:focus-visible`，全局已定義

- [ ] **Dark mode 自動支援**：所有 token 已在 Light/Dark 兩套主題中定義，只要使用 `var(--da-*)` 即自動適配主題。無需編寫 `@media (prefers-color-scheme: dark)` 媒體查詢

- [ ] **更新 tool-registry.yaml**：在 `docs/assets/tool-registry.yaml` 中註冊新工具（metadata、category、icon 等）

- [ ] **同步 jsx-loader CUSTOM_FLOW_MAP**：若工具在 Guided Flows 中提及，應在 `jsx-loader.html` 的 `CUSTOM_FLOW_MAP` 中新增對應的 JSX 檔案路徑

### ⚠️ 常見陷阱

1. **Inline color 而非 var()**：`<div style="color: #333;">` ❌ 應改為 `<div style="color: var(--da-color-fg);">` ✅

2. **忘記 rgba 適配**：在 Dark 模式中，opacity 可能需調整。使用 token 時應信任 token 已為兩套主題優化

3. **`class="dark"` 衝突**：若工具引用了 Tailwind 或其他框架的 `dark:` prefix，應改為 `[data-theme="dark"]` 選擇符，並確保選擇符優先級正確

4. **localStorage 未妥善 fallback**：某些隱私瀏覽模式禁用 localStorage，應實作 try-catch

---

## 10. 舊工具遷移指引

對於 v2.6.0 遺留的 hardcoded CSS 工具，遷移步驟如下：

### 10.1 色彩遷移

將 inline hex colors 或 CSS 中的硬編碼色彩替換為 token：

**Before:**
```jsx
<div style={{ color: '#1f2937', backgroundColor: '#f3f4f6' }}>
  Alert severity
</div>
```

**After:**
```jsx
<div style={{
  color: 'var(--da-color-fg)',
  backgroundColor: 'var(--da-color-bg-elevated)'
}}>
  Alert severity
</div>
```

### 10.2 間距遷移

將 hardcoded spacing 值改為 token：

**Before:**
```jsx
<div style={{ padding: '16px 24px', marginBottom: '12px' }}>
  Content
</div>
```

**After:**
```jsx
<div style={{
  padding: 'var(--da-space-md) var(--da-space-lg)',
  marginBottom: 'var(--da-space-sm)'
}}>
  Content
</div>
```

### 10.3 焦點樣式遷移

移除各工具中自行定義的 `:focus-visible`（已在全局提供）：

**Before:**
```css
.my-button:focus-visible {
  outline: 2px solid #3b82f6;
  outline-offset: 2px;
}
```

**After:**
```css
/* 刪除此規則，依賴全局 *:focus-visible */
```

### 10.4 向下相容性

**index.html（Tools Hub）** 應保留 legacy 色彩別名，以支援尚未遷移的工具：

```css
/* index.html 中的 legacy aliases */
:root {
  --bg: var(--da-color-bg);
  --fg: var(--da-color-fg);
  --primary: var(--da-color-primary);
  --success: var(--da-color-success);
  --warning: var(--da-color-warning);
  --error: var(--da-color-error);
  /* 等等... */
}
```

新工具應直接使用 `--da-*`；舊工具在遷移期間可繼續使用 legacy aliases。

---

## 11. 檔案結構

```
docs/
├── assets/
│   ├── design-tokens.css          ← 唯一 SSOT
│   ├── tool-registry.yaml         ← 工具 metadata
│   └── ...
├── interactive/
│   └── index.html                 ← Tools Hub（消費方）
└── jsx-loader.html                ← JSX 工具載入器（消費方）
```

---

## 12. 版號與維護

此設計系統隨平台整體版本升級。當 v2.6.0 升級至 v2.7.0 時：

1. 更新 `design-tokens.css` 內的 token 值（若有調整）
2. 更新此文件（`design-system-guide.md`）的 `version` frontmatter
3. 在 `CHANGELOG.md` 記錄設計系統變更
4. 若新增 token category，更新本文 § 3-7 的對照表
5. 運行 `make lint-docs` 確保文件品質

---

## 相關文件

- [`docs/assets/design-tokens.css`](../assets/design-tokens.css) — Token 定義（SSOT）
- [`docs/assets/tool-registry.yaml`](../assets/tool-registry.yaml) — 互動工具註冊表
- [`docs/interactive/index.html`](../interactive/index.html) — Tools Hub
- [`docs/assets/jsx-loader.html`](../assets/jsx-loader.html) — JSX 工具載入器
- [`docs/internal/tool-map.md`](tool-map.md) — 所有工具清單
- [`CHANGELOG.md`](../../CHANGELOG.md) — 版本歷程

---

**最後更新**：v2.6.0 Phase .a0
**作者群**：Dynamic Alerting 核心團隊
