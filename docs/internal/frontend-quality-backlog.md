---
title: "前端品質待辦 (Frontend Quality Backlog)"
tags: [internal, testing, playwright, a11y, quality]
audience: [maintainers, ai-agent]
version: v2.9.0
lang: zh
---

# 前端品質待辦 (Frontend Quality Backlog)

> **目的**：登記所有 `test.fixme()` / `test.skip()` / a11y waiver / axe exclude 等**故意放過的前端品質債務**，避免「先 fixme、之後再說」的雪球債（v2.7.0 Phase .e review 發現此 pattern 後定規於 [`testing-playbook.md` §v2.7.0 LL §2](testing-playbook.md#2-testfixme-是債務標記不是先通過-ci的工具)）。
>
> **停下的 E2E 測試不在本表登記**（#1449）：登記就是測試上的 `issue` annotation。
>
> - **寫法**：`test.fixme('title', { annotation: { type: 'issue', description: '#<n>' } }, fn)`。
> - **判定**：`tests/e2e/reporters/parked-tests.ts` 讀 runner 實際跑出的結果。結果為 skipped、或預期狀態不是 passed 的測試，沒有掛 `#<數字>` 形式的 issue，整次執行就判為失敗。
> - **涵蓋**：判的是執行結果而不是寫法，所以別名、計算式呼叫、`testInfo.skip()`、`test.fail` 都在範圍內。
> - **列出全部**：`cd tests/e2e && npx playwright test --list --reporter=json` 列得出所有宣告式的停車測試。
> - **ESLint 的角色**：只擋 `.skip()`（任何形式），作為 commit 時的快速回饋。
> - **看不到的**：沒被收進或沒被執行的測試，例如 `--grep`、`--grep-invert`、`testIgnore`、`--shard` 排除的，以及刪掉的測試。也不檢查 issue 是否還 open。
> - **執行點**：本機每次 `playwright test`，以及 CI 的 `Smoke Tests (Chromium)`。⚠️ 該 job 不在 main 的 required checks 內，紅了不擋 merge。

## 登記規則

1. **停下的 E2E 測試**：不在本表，見上方。`.skip()` 一律不准，環境差異走 `--grep` tag 或 playwright.config.ts 的 project。
2. **axe-core violation 寫 `allowedNonCriticalViolations > 0`** — 同等登記，註明放過的 violation 類型 + 預計修復版本。
3. **Playwright `exclude` / `route` stub 長期存在** — 超過 1 個 minor 版本未銷案者登記。
4. **跨版本殘留**：任何項目跨超過 1 個 minor 版本（如 v2.7.0 引入、v2.8.0 還在）**必須**走 [`testing-playbook.md` §v2.7.0 LL §2](testing-playbook.md#2-testfixme-是債務標記不是先通過-ci的工具) 的 knowledge annealing 三選一（calibrate / 刪測，或改為帶 issue 的 `test.fixme` / 用 `--grep` tag 或 playwright.config.ts 的 project 把它移出預設 run）。⛔ 第三項原本寫「改 `test.skip` 寫明放棄原因」——實測那個寫法會被 lint 擋（#1428），是個做不到的動作。

## 登記 Template

```
| 項目 | Test 名 / 檔案 / 行號 | 類型 | 原因 | 引入版本 | 預計移除 | Owner |
|------|------|------|------|------|------|------|
| ... | wizard.spec.ts:42 "validates deployment step" | test.fixme | locator 要 --ui 校準 | v2.7.0 | v2.8.0 | @maintainer |
```

## 目前待辦（active）

停下的 E2E 測試以 `--list` 為準（見上方）。目前是 `visual.spec.ts` 的 5 支 baseline 測試，由 [#3642](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3642) 追蹤；它們原本以 block comment 停著，#1449 起改為宣告式 fixme。其他類型（axe waiver、長期 stub）：_無登記項目。_

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

> ⛔ **errata（#1428）**：上面這句在寫下的當時就不成立。`make lint-e2e` 當時**沒有任何呼叫端**（全 repo 只有 target 自身宣告與這一行提及），而「不需再等 CI」的實情是**根本沒有 CI 那一層**——真正的唯一執行點是本機 hook，且它在任何 `git worktree` 裡都因缺 `node_modules` 而失敗。#1428 之後三個入口（hook / `make lint-e2e` / `playwright.yml` 的 `E2E Spec Lint (A-13)` job）都改走同一支 `scripts/tools/lint/e2e_spec_lint.sh`，並由 `tests/lint/test_e2e_spec_lint.py` 釘住。⚠️ **`make lint-e2e` 仍然沒有自動觸發點**——它是人手動打的入口，#1428 沒有改變這件事，只是讓它不再與另外兩個分岔。現況以本頁開頭那段為準。

## 相關資源

| 資源 | 用處 |
|------|------|
| [`testing-playbook.md` §v2.7.0 LL](testing-playbook.md) | Locator calibration 五步 checklist、Locator 穩定性優先順序（§v2.7.0 LL §1-§5）|
| [`dev-rules.md` §T 工具生命週期](dev-rules.md) | 工具 active → deprecation → archive 四態 |
| [`test-coverage-matrix.md`](test-coverage-matrix.md) | Tier 1/2/3 工具清單（決定哪個工具需要 E2E spec） |
