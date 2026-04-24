---
title: "前端品質待辦 (Frontend Quality Backlog)"
tags: [internal, testing, playwright, a11y, quality]
audience: [maintainers, ai-agent]
version: v2.7.0
lang: zh
---

# 前端品質待辦 (Frontend Quality Backlog)

> **目的**：登記所有 `test.fixme()` / `test.skip()` / a11y waiver / axe exclude 等**故意放過的前端品質債務**，避免「先 fixme、之後再說」的雪球債（v2.7.0 Phase .e review 發現此 pattern 後定規於 [`testing-playbook.md` §v2.7.0 LL §2](testing-playbook.md#2-testfixme-是債務標記不是先通過-ci的工具)）。
>
> **搭配的自動化**（v2.8.0 A-13）：ESLint `eslint-plugin-playwright/no-skipped-test` 已在 `tests/e2e/eslint.config.mjs` 設 `disallowFixme: true` + `allowConditional: false`，新加 `test.fixme()` / `test.skip()`（bare form）會被 pre-commit `playwright-lint` hook 或 CI 直接拒。登記**必須**發生在放 fixme 的 commit 前或同一 commit 內。

## 登記規則

1. **新增 `test.fixme()` / `test.skip()`（bare form）** — 此檔登記為**硬性條件**。條件式 `test.skip(!isLinux, 'reason')` 不屬於債務，不需登記。
2. **axe-core violation 寫 `allowedNonCriticalViolations > 0`** — 同等登記，註明放過的 violation 類型 + 預計修復版本。
3. **Playwright `exclude` / `route` stub 長期存在** — 超過 1 個 minor 版本未銷案者登記。
4. **跨版本殘留**：任何項目跨超過 1 個 minor 版本（如 v2.7.0 引入、v2.8.0 還在）**必須**走 [`testing-playbook.md` §v2.7.0 LL §2](testing-playbook.md#2-testfixme-是債務標記不是先通過-ci的工具) 的 knowledge annealing 三選一（calibrate / 刪測 / 改 `test.skip` 寫明放棄原因）。

## 登記 Template

```
| 項目 | Test 名 / 檔案 / 行號 | 類型 | 原因 | 引入版本 | 預計移除 | Owner |
|------|------|------|------|------|------|------|
| ... | wizard.spec.ts:42 "validates deployment step" | test.fixme | locator 要 --ui 校準 | v2.7.0 | v2.8.0 | @maintainer |
```

## v2.8.0 — 目前待辦（active）

_無登記項目。v2.8.0 PR #57 A-7 清零後，`test.fixme()` / `test.skip()`（bare form）在 E2E specs 為零，ESLint 強制守關。_

## 歷史清零紀錄

### v2.8.0 — A-7 Phase .a locator calibration sprint（PR #57, 2026-04-24）

清零 **8 個** `test.fixme()`，4 個 spec × 2 fixme each（cowork / main worktree sync pattern + Dev Container headless `--repeat-each=3` gate）：

| Spec | 原 Test 名 | 原因 | 校準後 locator | 學到什麼 |
|------|------|------|------|------|
| `notification-previewer.spec.ts:29` | renders channel selector or notification types | 寬 `:text-matches` 可能 0 match | `getByRole('button', { name: /^Select receiver:/i })` + count ≥ 5 | 所有 receiver 按鈕都有 `aria-label="Select receiver:<Label>"` prefix — 最穩的 semantic anchor |
| `notification-previewer.spec.ts:46` | preview area is present | 多層 OR locator 命中率低 | `getByLabel(/notification (title\|body) preview/i)` + count ≥ 2 | LivePreview component 有 title + body 兩個 preview box，都帶 `aria-label` |
| `threshold-heatmap.spec.ts:30` | renders heatmap grid or table | 通用 `table` 可能撞到其他表 | `getByRole('table', { name: /threshold distribution/i })` | component 已加 `role="table" aria-label="Threshold distribution table"`，單一精準 match |
| `threshold-heatmap.spec.ts:47` | colorblind-safe severity symbols | 複雜正規表達式 + 空 data fallback 邏輯混亂 | `getByText('✓', { exact: true })` + anySeverity fallback | ADR-012 hotfix 產生 106 個 ✓/⚠/❌ `<span aria-hidden="true">`，取簡潔訊號 |
| `rbac-setup-wizard.spec.ts:29` | renders role or permission selection | 「Admin」字串在 step 1 不存在 | `getByText('Set Permissions', { exact: true })`（step nav 鎖）| **原假設錯**：wizard 先 Define Groups，step 3 才 Set Permissions。test 意圖改為「step nav 有 permission step」 |
| `rbac-setup-wizard.spec.ts:46` | toggle or checkbox for permissions is interactive | step 1 無 toggle/checkbox | `getByRole('button', { name: /Next/i })` + `/Reset/i` | 同上，interactive controls 在每個 step 的 nav 層，非 step 1 body |
| `cicd-setup-wizard.spec.ts:29` | renders CI/CD provider selection | 寬 `:text-matches` 可能 0 match | `getByText('GitHub Actions', { exact: true })` | step 1 provider card 以 exact text 渲染，最精準 |
| `cicd-setup-wizard.spec.ts:46` | YAML output or config preview section exists | YAML 只在 step 5 生成，step 1 不存在 | `getByText(/Review & Generate/)` | 改斷言「wizard pipeline 已接上 Review step」而非「YAML 已渲染」— 校準深度不夠時把 test 重新 frame |

**方法論**：Cowork → Dev Container 路徑切換
- 原計畫：Cowork 側 JSX 靜態分析 best-guess → 交 user Windows host `--ui` 目視驗證
- 實際走：Dev Container 裝 chromium + system deps → 寫 calibration probe `_calibrate.mjs`（`_*` 前綴 scratchpad，用完手動刪；ESLint config 的 `ignores` 對 `_*.mjs` 有 cover，repo `.gitignore` 只 cover `_*.json`/`_*.out`/`_*.err`，若需頻繁 probe 可考慮擴 ignore pattern）→ 對每個 candidate 列印 `{count, textContent, outerHTML}` → 取 `count === 1 or 恰好受控` → headless `npx playwright test --repeat-each=3` 驗穩定（48/48 PASS 驗證）
- 教訓：**Playwright Node.js API 直接查 count/textContent 等同 `--ui` locator panel 信號**；不需 X11 GUI。

**自動化守關**：同 PR 一併落地 A-13 ESLint（`tests/e2e/eslint.config.mjs` + pre-commit `playwright-lint` hook + Makefile `lint-e2e`），之後再有新 `test.fixme()` 進來會被 **commit-time** 擋下，不需再等 CI 或 review catch。

## 相關資源

| 資源 | 用處 |
|------|------|
| [`testing-playbook.md` §v2.7.0 LL](testing-playbook.md) | Locator calibration 五步 checklist、Locator 穩定性優先順序（§v2.7.0 LL §1-§5）|
| [`dev-rules.md` §T 工具生命週期](dev-rules.md) | 工具 active → deprecation → archive 四態 |
| [`test-coverage-matrix.md`](test-coverage-matrix.md) | Tier 1/2/3 工具清單（決定哪個工具需要 E2E spec） |
