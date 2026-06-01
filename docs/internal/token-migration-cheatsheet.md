---
title: "Token Migration Cheat Sheet"
tags: [internal, design-tokens, migration]
audience: [maintainer]
version: v2.8.1
lang: zh
---
# Token Migration Cheat Sheet（#444 Phase 1）

把 JSX 工具裡 hardcoded 的 hex 色碼 / px 數值對應到 `var(--da-*)` design token。
**強制前置**：遷移前先查本表決定 token，不要瞎猜（猜錯會破壞 Design System 語意）。

> **⏳ 生命週期 / Deprecation criteria**：本文件分兩層——
> **易腐層**（下方 HEX→token、PX→token 對照表，綁定 #444 當下盤點的 14 檔/70
> violations）在 **#444 Phase 1 遷移收尾後刪除**（屆時這些檔已清乾淨，留著會誤導新人）。
> **耐久層**（「決策準則」§語意優先、px layout 豁免原則、FIXME fallback 流程）屆時
> **上抬合併進 [`lint-policy.md`](lint-policy.md)**，本檔整份移除或移至 `archive/`。
> 觸發點：`check_design_token_usage.py --full-scan` 回報 0 violations 時。

- Token 定義端 SSOT：[`docs/assets/design-tokens.css`](../assets/design-tokens.css)（light + dark 兩套，名稱相同、值不同 → **用 token 名才能自動跟著 dark mode 切換**，這正是不可硬寫 hex 的核心理由）。
- Gate：`scripts/tools/lint/check_design_token_usage.py`（pre-commit + `ci.yml`，diff-only）。
- 豁免：行末 `/* token-exempt */`、`#fff`/`#000`/`#ffffff`/`#000000`、`0/1/2px`（border/hairline）、純註解行。

## 決策準則（先讀）

1. **語意優先於數值**。先問「這個顏色**代表什麼**」，再選 token —— 例如錯誤紅選 `--da-color-error`，不是看到 `#ef4444` 就硬配同值 token。同一個 hex 在不同語境可能對到不同 token。
2. **找不到語意對應**時，退而求其次配「視覺最接近且語意不衝突」的 token。
3. **完全無對應**（罕見的一次性色）：暫時保留原值 + 行末加 `/* FIXME: no token match (#444) */`，並在本檔末「待補 token 候選」區登記，留待 design-tokens.css 補 token。**不要**為單一用途硬塞一個語意不明的新 token。

## HEX → token 對照（依本次 14 檔實際出現值）

> 值取自 light theme；dark theme 同名 token 自動切換。語意欄是建議用途，仍以實際語境為準。

| Hardcoded hex | 建議 token | 語意 / 備註 |
|---|---|---|
| `#2563eb` | `var(--da-color-accent)` | 主強調藍（= accent / info 同值）|
| `#3b82f6` | `var(--da-color-card-hover-border)` | hover 邊框藍；若是純文字連結看語境可用 accent |
| `#1e40af` | `var(--da-color-accent-hover)` | 深一階 accent（hover/active）|
| `#dbeafe` / `#eff6ff` | `var(--da-color-accent-soft)` | 淺藍底（soft accent / info-soft）|
| `#1e293b` | `var(--da-color-toast-bg)` | 深底（toast / 深色 surface）；若是深色文字看語境 |
| `#0f172a` | `var(--da-color-hero-bg)` | hero 深底 |
| `#dc2626` / `#ef4444` | `var(--da-color-error)` | 錯誤紅 |
| `#fef2f2` / `#fecaca` | `var(--da-color-error-soft)` | 錯誤淺底 |
| `#991b1b` | `var(--da-color-error-text)` | 錯誤文字（深紅）|
| `#f59e0b` | `var(--da-color-warning)` | 警告橘 |
| `#fffbeb` / `#fef3c7` | `var(--da-color-warning-soft)` | 警告淺底 |
| `#92400e` | `var(--da-color-warning-text)` | 警告文字（深棕）|
| `#10b981` / `#16a34a` | `var(--da-color-success)` | 成功綠（icon-validation 同值 `#10b981`）|
| `#ecfdf5` / `#d1fae5` / `#f0fdf4` | `var(--da-color-success-soft)` | 成功淺底 |
| `#065f46` | `var(--da-color-journey-monitor)` | 深綠（monitor 語境）；一般成功深字可用 success-text 視語意 |
| `#8b5cf6` | `var(--da-color-icon-rules)` | 紫（rules icon）|
| `#5b21b6` / `#ede9fe` | `var(--da-color-icon-rules)` / soft 對應 | 紫系深/淺 |
| `#6b7280` | `var(--da-color-tile-muted)` | 次要灰文字 |
| `#475569` | `var(--da-color-muted)` | muted 文字 |
| `#374151` / `#111827` | `var(--da-color-fg)` | 主前景深字（依深淺取最近）|
| `#e5e7eb` / `#cbd5e1` / `#f3f4f6` | `var(--da-color-surface-border)` / `--da-color-tag-bg` | 邊框 / 淺灰底（看用途）|

> 上表未涵蓋的 hex → 套「決策準則」第 1/3 步處理。掃描誤判（`#153`/`#100`/`#160` 等三位數其實是被切到的非色碼 token，如 `width:150` 之類）在遷移時人工確認，多半屬 px 類或誤報，可加 `/* token-exempt */`。

## PX → token 對照

> 設計系統若無 spacing token scale，則 px 多為一次性 layout 尺寸（`maxWidth`/`minHeight` 等），不一定有對應 token。原則：

| 情境 | 處理 |
|---|---|
| `fontSize` 的 px（如 `11px`/`12px`/`14px`）| 優先用字級 token（若 design-tokens.css 有 `--da-font-size-*`）；無則 `/* token-exempt */` 並登記待補 |
| layout 尺寸（`100px`/`120px`/`150px`/`900px` 的 width/height/maxWidth）| 多為一次性版面值，無語意 token → 加 `/* token-exempt */`（這是 gate 認可的豁免，非技術債）|
| spacing（padding/margin/gap）| 若有 `--da-space-*` scale 則用之；無則 token-exempt |

**注意**：`check_design_token_usage.py` 的 px 偵測只在 `style=` 物件內、且排除 `0/1/2px`。多數 layout px 用 `token-exempt` 標註即可清掉，不需硬造 token。

## 待補 token 候選（遷移時發現無對應就登記到這）

<!-- 格式：- `<hex/px>` @ `<file:line>` — <為何無對應 / 建議補什麼語意 token> -->

（目前空 —— Phase 1 遷移時若遇到無對應值，在此累積，作為 design-tokens.css 後續擴充輸入）

## 參考

- 完成案例：PR #34 Token Audit（112 refs）、PR #58 wizard.jsx migration
- Phase 0 gate 修復：PR #722
- Issue：#444
