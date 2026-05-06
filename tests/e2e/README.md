---
title: "tests/e2e — Playwright E2E 起手式（深入版）"
purpose: |
  Playwright spec 作者的速查：tag 怎麼選、fixture 怎麼用、新 spec 從哪裡開始。
  父 README（tests/README.md）回答「我要寫什麼測試該擺哪」；本檔回答
  「進到 tests/e2e/ 後，怎麼下手」。

  歷史：本檔 v1 寫於 2024 只列 5 個 critical path spec，已嚴重過時。
  v2（2026-05，TD-032a）對齊現況：24 specs / 136 tests / 7 fixtures。
audience: [contributors, ai-agent]
lang: zh
---

# tests/e2e — Playwright E2E 起手式（深入版）

父 README：[`tests/README.md`](../README.md)。先看父檔的決策樹（測試擺哪、CI 對應、跑法 cheat sheet）；本檔處理進到 `tests/e2e/` 之後的細節：tag 語意、fixture 契約、新 spec 模板、REG 編號制度。

## 目前規模（2026-05）

- **24 specs / 136 tests**（chromium-only smoke + 互動）
- **5 fixtures**（`fixtures/`）：portal-tool-smoke / axe-helper / diagnostic-matchers / mock-data / test-helpers
- **2 規範 tag**：`@critical`（22 specs）/ `@visual`（1 spec）
- **a11y 是預設 gate**：`runToolSmokeChecks` 預設 `allowedNonCriticalViolations: 0` + `skipA11y: false`，13 個 spec 已強制過 axe

## Tag taxonomy（測試標籤語意）

掛在 `test.describe(..., '<title> @<tag>')` 上。本目錄目前**只有兩個有效 tag**：

| Tag | 語意 | 使用時機 | 跑的指令 |
|-----|------|---------|---------|
| `@critical` | smoke + sanity 主要互動 + REG 防線；CI 必跑 | 工具的「載入 + 主要互動鏈路 + 回歸防線」 | `npm run test:critical` 或預設 `npm test` |
| `@visual` | pixel-diff baseline | `toHaveScreenshot()` 比對；只能在 Linux baseline 平台跑 | `npm run test:visual`（基線更新 `test:visual:update`） |

**沒被掛 tag 的 spec**會被 `npm test`（`--grep-invert @visual`）跑到，但不被 `test:critical` 篩到。寫新 spec 時：

- portal 工具的 smoke + 主要互動 → **`@critical`**
- 鎖視覺基線 → 寫進 `visual.spec.ts`（不要新開 file），`@visual` tag
- diagnostic 自我測試 / error boundary 等基礎設施驗證 → 不掛 tag（仍會被 default run 跑到）

⛔ 不要發明新 tag。如果要分流（例如 `@slow` / `@nightly`），先在這個 README 註冊語意。

## Fixture contracts（每個 fixture 做什麼、不做什麼）

| Fixture | API | 做什麼 | 不做什麼 |
|---------|-----|--------|----------|
| `portal-tool-smoke.ts` | `loadPortalTool(page, key)` | navigate 到 `../assets/jsx-loader.html?component=<key>`，等 `document.title` mount + `networkidle` | 不 mock API；不點任何按鈕；不等特定 testid |
| | `runToolSmokeChecks(page, opts)` | title regex match + 無 "Failed to load" + axe WCAG 2.1 AA（**0 critical violations** 預設嚴格 gate） | 不測互動；不測 form submission |
| | `assertNoAbsoluteRootHrefs(page)` | 找出 `href="/foo"` 樣式的絕對根路徑（REG-004 風險） | 不檢查 fragment / external link |
| `axe-helper.ts` | `checkA11y(page, opts)` | 跑 axe-core WCAG 2.1 AA scan，回傳 `violations` | 不自動 fail；caller 自己決定 budget |
| | `waitForPageReady(page, sel?)` | 等 networkidle + 可選的 selector visible（CI Python http.server 慢需要） | 不等 React mount 完成 |
| `diagnostic-matchers.ts` | `toBeVisibleWithDiagnostics()` Playwright matcher | element 找不到時 dump 所有 `[data-testid]` 與 `aria-label` 到失敗訊息（S#98 / LL §11） | 不取代 `toBeVisible`；只在 cold-start 風險點用 |
| `mock-data.ts` | `mockUser` / `mockTenants` / `mockGroups` / `mockAlerts` / `mockBatchOperationResponse` 等常數 | 提供共用 mock fixture | 不自動掛 `page.route` |
| `test-helpers.ts` | `waitForPortalReady(page)` / `mockApiEndpoint(...)` 等 | portal 級操作（不專屬單一 portal 工具） | 不取代 `portal-tool-smoke` 的 navigate |

**選擇順位**：portal JSX 工具 spec → 先用 `loadPortalTool` + `runToolSmokeChecks`；只有需要 URL 參數 / 多步互動時再下放到 raw `page.goto`。

## 新 portal 工具 smoke spec 模板

**最小可運行 spec**（套上 `runToolSmokeChecks` 等於同時拿到 a11y gate + 404 sentinel）：

```typescript
import { test } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('<Tool Display Name> @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, '<tool-registry-key>');
    await runToolSmokeChecks(page, {
      // Optional: title regex (omit if tool has no consistent title contract)
      expectedTitleMatch: /<keyword from tool>/i,
    });
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, '<tool-registry-key>');
    await assertNoAbsoluteRootHrefs(page);
  });
});
```

加互動測試（fill / click）時繼續放在同一個 `describe` 內，新 `test()` block。**不要**為了單一互動 spec 另開 file。

## REG-NNN regression-guard 命名

明確的回歸防線 test 用 `REG-NNN` 編號標記在 test 名稱或 fixture docstring：

| 編號 | 防的是 |
|------|--------|
| REG-001 | （reserved — 在 source comments 內標註的第一個 cataloged regression） |
| REG-004 | **portal-safe hrefs**：絕對根路徑 `href="/foo"` 在 portal sub-path 部署會 404；`assertNoAbsoluteRootHrefs` helper 防守此類 |

**新 regression test 的 SOP**：
1. 找下一個沒用過的 `REG-NNN`（`grep -rhoE "REG-[0-9]+" tests/ docs/interactive/` 確認）
2. test 名稱寫 `'<behavior> (REG-NNN regression guard)'`
3. spec / fixture 內若有專用 helper，docstring 寫 `(see REG-NNN)`
4. 在這個表格新增一行說明

## Mocking 慣例

- `page.route('**/api/v1/<endpoint>', ...)` 在 spec 開頭 `beforeEach` 或 navigate 前掛
- mock data 優先放 `fixtures/mock-data.ts`；spec 專屬的 inline 即可
- 預設**不 mock**——`@critical` 路徑與 deterministic 要求的 spec 才 stub
- 8 / 24 spec 用 `page.route`，其他依賴 dev container 的 mock backend

```typescript
// 完整範例
await page.route('**/api/v1/me', async (route) => {
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ email: 'test@example.com', permissions: { global: ['read'] } }),
  });
});
```

## Setup

```bash
cd tests/e2e
npm install
npx playwright install chromium
```

## 跑

完整指令清單看父 README 的 cheat sheet。本目錄局部：

```bash
npm test                       # @critical + 未 tag spec（排除 @visual）
npm run test:critical          # 只跑 @critical
npm run test:visual            # 跑 @visual（需要 Linux baseline）
npm run test:visual:update     # 更新 baseline（只能 Linux 或 dev container 跑）
npm run test:ui                # 互動模式
npm run test:headed            # 看瀏覽器
npm run test:debug             # 步進
npm run lint                   # eslint（A-13 fixme/skip guard）
```

環境變數：

```bash
BASE_URL=http://my-portal.dev npm test   # 自訂 portal URL（預設 http://localhost:8080/interactive/）
DEBUG=pw:api npm test                    # 開啟 debug log
CI=true npm test                         # CI 模式（1 retry, 1 worker）
```

## CI 對應

GitHub Actions：`.github/workflows/playwright.yml`（Smoke Tests (Chromium) job）。觸發：push to main、PR。本目錄全部以 `--grep-invert @visual` 跑（`npm test`）；視覺基線由 `visual-baseline.yaml` 的 `workflow_dispatch` 手動更新。

## Debug

- **Element not found**：`npm run test:ui` 看 selector；`toBeVisibleWithDiagnostics()` 失敗時自動 dump 所有 testid（見 fixture contract 表）
- **Cold-start 風險**：先看 [testing-playbook.md §LL §11](../../docs/internal/testing-playbook.md) 的 cold-start contract
- **CI flake / hang**：先看 [testing-playbook.md §LL §13](../../docs/internal/testing-playbook.md)（CI E2E hang → reproduce locally first）
- **截圖**：失敗時自動存 `test-results/`；HTML report 跑 `npx playwright show-report`

## Best Practices（局部，互補）

父 README 已說明跨類別共通原則。本目錄專有的：

1. **portal 工具優先用 `loadPortalTool` + `runToolSmokeChecks`**——別再 `page.goto('../assets/jsx-loader.html?...')` 自己寫了。fixture 已經處理 `document.title` 等待 + axe gate + 404 sentinel。
2. **`data-testid` 優於 CSS class**——portal 元件已普遍 attach。
3. **`waitForPageReady` 用於 cold-start 場景**——CI 的 Python http.server 慢，networkidle 可能 timeout 到 axe 跑空白頁。

## Related

- [Testing Playbook](../../docs/internal/testing-playbook.md) — CI flake / Go race / Playwright timeout 排錯
- [test-coverage-matrix.md](../../docs/internal/test-coverage-matrix.md) — E2E 場景 × 功能域覆蓋矩陣
- [Playwright docs](https://playwright.dev/docs/intro)
