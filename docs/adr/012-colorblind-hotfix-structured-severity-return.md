---
title: "ADR-012: threshold-heatmap 色盲補丁 — 結構化 severity 返回值"
tags: [adr, accessibility, wcag, portal, v2.7.0]
audience: [frontend-developers, design-system-maintainers]
version: v2.7.0
lang: zh
---

# ADR-012: threshold-heatmap 色盲補丁 — 結構化 severity 返回值

> **Language / 語言：** **中文 (Current)** | [English](./012-colorblind-hotfix-structured-severity-return.en.md)

> Originally recorded as **DEC-L (Sprint 0)** in `docs/internal/v2.7.0-planning.md §19`.
> Hotfix 已於 Day 4 AM 落地；本 ADR 保存決策脈絡以便未來 generalize 到其他
> 顏色敏感工具（platform-demo mode badges, health dashboard tier badges 等）。

## 狀態

✅ **Accepted**（v2.7.0 Day 4 Sprint 0, 2026-04-16）— hotfix 已 land；後續 runtime WCAG 驗證 CI-gated。

## 背景

### 原始實作的缺陷

`threshold-heatmap.jsx` v2.6.0 實作中，cell severity 以三色 palette 表達
（green-200 / yellow-200 / orange-200 / red-500）。這違反 **WCAG 1.4.1 Use
of Color**：資訊只透過色彩傳遞，紅綠色盲使用者無法區分「中」與「異常值」。

### 為什麼是 Sprint 0 而非完整遷移

1. 完整 palette → token 遷移（87 tailwind palette → 0）需 ~3 hr，會阻擋 Phase .a0 批次 4 時程
2. WCAG 1.4.1 是法遵風險（AA 要求），優先**補救語意**，**顏色 token 遷移延後**
3. 補丁落地後，threshold-heatmap 的 token 正式遷移仍為 Phase .a0 的收束任務（§19 Day 5 候選 #5）

## 決策驅動力

- 必須同時滿足「視覺符號」與「螢幕閱讀器」兩種 accessibility channel
- 單一 callsite（cell render）要能一次取得 color class / symbol / tier label，避免 3 個平行 if-else 樹
- 未來其他工具（platform-demo badges）可直接 re-import 這個 helper

## 決策

把 cell severity 判定邏輯抽成 `getCellSeverity(value, stats)` 函式，**返回結構化物件**：

```jsx
function getCellSeverity(value, stats) {
  if (value > stats.p95) {
    return {
      colorClass: 'bg-red-500 text-white',
      symbol: '❌',
      tier: 'outlier',
      ariaLabel: t('異常值', 'Outlier'),
    };
  }
  if (value > stats.mean + stats.stddev * 2) {
    return { colorClass: 'bg-orange-200 text-orange-900', symbol: '⚠⚠', tier: 'high', ariaLabel: t('高', 'High') };
  }
  if (value > stats.mean) {
    return { colorClass: 'bg-yellow-200 text-yellow-900', symbol: '⚠', tier: 'medium', ariaLabel: t('中', 'Medium') };
  }
  return { colorClass: 'bg-green-200 text-green-900', symbol: '✓', tier: 'low', ariaLabel: t('低', 'Low') };
}
```

### Render 端

- cell outer: `<td aria-label={ariaLabel} className={colorClass}>`
- cell inner: `<span aria-hidden="true">{symbol}</span>{value}`

此設計確保：
- **螢幕閱讀器** 朗讀 `ariaLabel`（"異常值 45.2"），不會 double-announce symbol
- **色盲使用者** 看到 Unicode symbol 而不僅是色彩
- **視覺使用者** 看到熟悉的 traffic-light 色彩 + symbol 冗餘

## 拒絕的替代方案

| 方案 | 拒絕原因 |
|---|---|
| 僅加 `aria-label`，不加 symbol | 色盲使用者螢幕上無法區分 |
| 僅加 symbol，不加 aria-label | 螢幕閱讀器會唸 "Warning Sign Warning Sign"（⚠⚠ 雙字元）——差 UX |
| 用 CSS `::before` 插入 symbol | 部分螢幕閱讀器不朗讀 ::before content；且 CSS pseudo 無法跟隨 React state |
| 改用 shape（方形 / 三角 / 圓） | 需要 SVG or icon library；未來主題化時會破功 |

## 後果

### 正面

- WCAG 1.4.1 **語意**達標（hotfix 層次；runtime 驗證 CI-gated）
- `getCellSeverity` helper 可複用至其他 severity-badge 工具
- 符號與顏色解耦，可獨立更換（e.g. 改用 ▲ ● ■ 之類）

### 負面 / 風險

1. **字型跨平台不一致**：⚠⚠ 在 Windows Consolas vs macOS SF Mono 寬度不一，表格對齊會漂。**緩解**：Day 5 Phase .a0 正式遷移時固定 monospace font stack，或改用 fixed-width Unicode block。
2. **`⚠⚠` 螢幕閱讀器朗讀**：NVDA / VoiceOver 會唸 "Warning Sign Warning Sign"。**緩解**：本 hotfix 靠 `ariaLabel` 覆蓋，但若未來其他 callsite 直接放 symbol 則需重提醒。
3. **Dark mode 對比度未驗證**：hotfix 使用 Tailwind palette（未遷移到 token），在 `[data-theme="dark"]` 下可能 contrast fail。**緩解**：Phase .a0 正式遷移時同步計算對比度。

### 需進一步追蹤

- `docs/internal/v2.7.0-day5-verification-triage.md` §3 列出 runtime 驗證的 CI gate
- Phase .a0 Day 5 候選 #5：threshold-heatmap palette → token 正式遷移

## 相關

- WCAG 2.1 — Success Criterion 1.4.1 Use of Color (Level A)
- `docs/interactive/tools/threshold-heatmap.jsx`
- `docs/internal/v2.7.0-planning.md` §19 DEC-L
- `docs/internal/v2.7.0-day5-verification-triage.md` §3
