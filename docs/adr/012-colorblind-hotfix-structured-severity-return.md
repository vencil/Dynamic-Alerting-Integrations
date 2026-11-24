---
title: "ADR-012: threshold-heatmap 色盲補丁 — 結構化 severity 返回值"
tags: [adr, accessibility, wcag, portal, v2.7.0]
audience: [frontend-developers, design-system-maintainers]
version: v2.9.0
lang: zh
id: ADR-012
tracking_kind: adr
status: accepted
domain: portal
created_at: 2026-04-18
updated_at: 2026-11-24
---
# ADR-012: threshold-heatmap 色盲補丁 — 結構化 severity 返回值

> **Language / 語言：** **中文 (Current)** | [English](./012-colorblind-hotfix-structured-severity-return.en.md)

## 狀態

✅ **Accepted**（v2.7.0, 2026-04-16）

2026-11-24 修訂：改寫為現行行為。決策不變。格子的顏色已從 Tailwind 調色盤改為 design token（`--da-color-*`），判定函式回傳的結構不受影響。

## 決策

**threshold-heatmap 每個格子的嚴重度，同時用三種管道表達：顏色、Unicode 符號、給螢幕閱讀器的文字標籤。** 三者由同一個函式 `getCellSeverity` 一次決定，回傳結構化物件，而不是在 render 端寫三套平行的 if-else。

[WCAG 1.4.1「Use of Color」](https://www.w3.org/WAI/WCAG21/Understanding/use-of-color.html)（Level A）要求資訊不能只靠顏色傳遞。符號讓色盲使用者在畫面上分得出等級；文字標籤讓螢幕閱讀器唸出等級；顏色只是輔助。

### 範例：格子數值 → 等級、符號、朗讀內容

`getCellSeverity(value, min, max, percentile95)` 先判斷是否高於 P95（所有格子數值的第 95 百分位數），否則依數值在 min–max 間的比例分級，回傳 `{ colorClass, symbol, tier }`：

```jsx {.quote src="tools/portal/src/interactive/tools/threshold-heatmap.jsx"}
function getCellSeverity(value, min, max, percentile95) {
  if (value === null || value === undefined) {
    return { colorClass: 'bg-[color:var(--da-color-surface)]', symbol: '', tier: 'none' };
  }

  // Flag outliers first (>P95)
  if (value > percentile95) {
    return {
      colorClass: 'bg-[color:var(--da-color-error)] text-[color:var(--da-color-card-bg)] font-bold',
      symbol: '\u274C', // ❌
      tier: 'outlier',
    };
  }

  const ratio = max === min ? 0 : (value - min) / (max - min);

  if (ratio < 0.33) {
// ...
```

以 min = 10、max = 90、P95 = 80 跑這個函式，加上把 `tier` 轉成中英文字的 `tierLabel(tier)`，得到：

| 格子數值 | 比例 | `tier` | 符號 | 朗讀的等級文字 |
|---|---|---|---|---|
| 20 | 0.125 | `low` | ✓ | 低 / Low |
| 45 | 0.44 | `medium` | ⚠ | 中 / Medium |
| 70 | 0.75 | `high` | ⚠⚠ | 高 / High |
| 85 | — | `outlier` | ❌ | 異常值 / Outlier |
| 無資料 | — | `none` | （無） | （無） |

render 端把文字標籤放在 `<td>` 上，符號用 `aria-hidden="true"` 藏起來，讓螢幕閱讀器只唸標籤、不重複唸符號：

```jsx {.quote src="tools/portal/src/interactive/tools/threshold-heatmap.jsx"}
                        const { colorClass, symbol, tier } = getCellSeverity(
                          value, stats.min, stats.max, stats.p95
                        );
                        const valueText = value ? value.toFixed(0) : '—';
                        const tierText = tierLabel(tier);
                        const ariaLabel = value
                          ? `${tenant} ${metric}: ${value.toFixed(2)}${tierText ? ', ' + tierText : ''}`
                          : `${tenant} ${metric}: ${t('無資料', 'no data')}`;
// ...
                            aria-label={ariaLabel}
                            role="gridcell"
                          >
                            {symbol && (
                              <span aria-hidden="true" className="mr-1 opacity-90">{symbol}</span>
                            )}
                            {valueText}
```

所以數值 70 的格子，畫面上是 `⚠⚠70`，螢幕閱讀器唸的是「`<租戶> <指標>: 70.00, High`」（中文介面唸「高」）。

min、max、P95 是對畫面上**所有**格子（所選租戶 × 所選 Rule Pack 的全部指標）一起算的，不是逐指標各算一次；這個工具顯示的是依 Rule Pack 預設值加隨機變動產生的範例資料。

## 背景

v2.6.0 的 threshold-heatmap 只用格子底色表達嚴重度（綠／黃／橙／紅）。這違反 WCAG 1.4.1：紅綠色盲使用者分不出「中」與「異常值」，螢幕閱讀器使用者則完全拿不到等級資訊。

修補時刻意分兩步：先補語意（加符號與文字標籤），把色彩改用 design token 留到之後另做，因為後者範圍大得多，而前者是 Level A 的合規缺口。token 遷移後來已完成，本 ADR 的決策不受影響。

設計時的要求：

- 視覺（符號）與螢幕閱讀器（文字）兩種管道都要滿足。
- render 端一次呼叫就拿到顏色、符號、等級，不維護三套平行的判斷。
- 符號與顏色解耦，日後換符號（例如 ▲ ● ■）不必動顏色。

## 後果與已知限制

### 得到的

- 每個有值的格子都有顏色以外的等級表示：畫面上的符號與朗讀的等級文字。
- 「符號＋文字，不只靠顏色」這個做法已延伸到其他地方，各有自己的測試守住：
  - Grafana dashboard `k8s/03-monitoring/fleet-threshold-distribution.json` 的樣本充足度與 Outliers panel，由 `tests/dx/test_fleet_threshold_dashboard.py` 檢查每個顏色階都有符號。
  - Grafana dashboard `k8s/03-monitoring/federation-revocation-dashboard.json` 的 stat panel，由 `tests/dx/test_federation_revocation_dashboard.py` 做同樣的檢查。
  - portal 的 `recipe-builder.jsx` 判定結果用形狀符號（`aria-hidden`）＋文字＋顏色表示。
- 延伸的是做法，不是函式：`getCellSeverity` 沒有 export，目前只有 `threshold-heatmap.jsx` 自己使用。

### 驗收與它的測試

- `tests/e2e/threshold-heatmap.spec.ts` 的 `colorblind-safe severity symbols are rendered (ADR-012)`：載入工具後，畫面上至少要有一個嚴重度符號（✓／⚠／❌）。它只檢查符號存在，不檢查數值與等級的對應；圖例本身就有 ✓，所以格子完全沒畫符號時它仍會通過。
- 同一支 spec 的 smoke 檢查跑 axe-core 的 WCAG 2 A／AA 規則：Critical 違規必須為零，非 Critical 最多容許一項。
- 這些 e2e 在每支 PR 的 `Smoke Tests (Chromium)` 中執行。
- `getCellSeverity` 的分級邊界沒有單元測試。

### 已知限制

1. **淺色主題下部分格子的文字對比不足。** 依 `docs/assets/design-tokens.css` 的 token 值計算：中、高等級格子的文字（`--da-color-warning` 疊在 `--da-color-warning-soft` 上）對比約 2.1:1，異常值格子（白字疊在 `--da-color-error` 上）約 3.8:1，都低於 AA 對一般文字要求的 4.5:1。同一份檔案已有為此設計的 `--da-color-warning-text`（標註 6.1:1），格子尚未改用。深色主題下同樣三組約為 9.0:1、5.3:1（低等級格子兩個主題都在 5:1 以上）。e2e 的 axe 檢查只跑預設主題，不涵蓋深色主題。
2. **中與高兩級的底色相同**（都是 warning token），彼此只靠符號 ⚠ 與 ⚠⚠、以及朗讀文字區分。
3. **圖例與實際分級有落差。** 圖例寫「高 (66–85%)」，但比例 0.85 以上、未超過 P95 的格子也歸為 `high`。
4. **數值為 0 的格子顯示不一致。** render 端用 `value ? … : …` 判斷有無資料，0 會顯示成 `—`、朗讀「無資料」，但 `getCellSeverity` 仍把它當成有值，畫出 ✓ 符號；計算 min／max／P95 時也會略過 0。
5. **新的呼叫端要自己守住兩件事**：符號放在 `aria-hidden="true"` 的元素裡，等級文字放在會被朗讀的地方。少了前者，螢幕閱讀器會把 ⚠⚠ 唸成兩次「warning sign」；少了後者，螢幕閱讀器拿不到等級。

## 考慮過的替代方案

| 方案 | 沒選的原因 |
|---|---|
| 只加 `aria-label`，不加符號 | 色盲使用者在畫面上仍分不出等級 |
| 只加符號，不加 `aria-label` | 螢幕閱讀器會逐字唸符號（⚠⚠ 唸成兩次「warning sign」），而且唸不出等級名稱 |
| 用 CSS `::before` 插入符號 | 部分螢幕閱讀器不朗讀 `::before` 內容；CSS 偽元素也無法直接跟著 React state 變化 |
| 改用形狀（方形／三角／圓） | 需要 SVG 或圖示庫，換主題時要另外處理形狀的顏色 |

## 相關

- [WCAG 2.1 — Understanding 1.4.1 Use of Color](https://www.w3.org/WAI/WCAG21/Understanding/use-of-color.html)
- 實作位置：`tools/portal/src/interactive/tools/threshold-heatmap.jsx`（`getCellSeverity`、`tierLabel`）
- 測試：`tests/e2e/threshold-heatmap.spec.ts`
- [ADR-014: wizard.jsx design token 遷移](014-wizard-arbitrary-value-token-migration.md) — 格子顏色所用 token 的遷移做法
- [ADR-015: `[data-theme]` 單軌 Dark Mode](015-data-theme-single-track-dark-mode.md) — 深色主題的切換機制
