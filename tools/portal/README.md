---
title: "Portal Build & Test (TECH-DEBT-030)"
purpose: |
  Foundation for Option C ES modules sweep — esbuild build pipeline +
  Vitest unit-test harness. PR-A of a multi-PR series; subsequent PRs
  migrate JSX tools from `window.__X` registration → ESM `export {}`.
audience: [contributors, ai-agent]
lang: zh
---

# Portal Build & Test (TD-030 foundation)

## 怎麼用

```bash
# 一次安裝（首次）
cd tools/portal && npm ci

# Build all entries listed in manifest.json into docs/assets/dist/
make portal-build

# Watch mode (dev iteration)
make portal-build-watch

# Run Vitest unit tests
make test-portal
```

## 結構

```
tools/portal/
├── package.json        # esbuild + Vitest + RTL deps
├── manifest.json       # list of tool entries to bundle (initially empty)
├── build.mjs           # esbuild script — strips frontmatter, bundles per-entry
├── vitest.config.ts    # Vitest config — jsdom + frontmatter strip plugin
├── test-setup.ts       # global mocks for window.__styles / window.__t / React
└── tsconfig.json       # TS config for both build and test files

docs/assets/dist/       # build output (per-tool ESM bundles)
tests/portal/           # unit test files (sibling to tools/, not under it)
```

## 為什麼 esbuild

- **Zero dev-server complexity**：`python -m http.server` 已經夠用，不需要 Vite dev server overhead
- **Deterministic output paths**：`docs/assets/dist/<entry>.js`，CI 重現性
- **One file**：`build.mjs` 80 行可全部讀懂
- **Fast cold builds**：本機完整重建 < 1s

## 為什麼 Vitest（不是 Jest / RTL only）

- **Native ESM support**：與 esbuild 對齊
- **Vite plugin ecosystem**：複用 `@vitejs/plugin-react` + 自訂 frontmatter strip
- **Watch mode**：開發時 instant feedback
- **jsdom + globals**：與 RTL 標準組合

## Manifest

新加一個工具到 ESM build：

1. 把工具從 `window.__X` 改成 `export { X }`
2. `tools/portal/manifest.json` 的 `entries` array 加入工具名（不含 `.jsx`）
3. 對應 HTML 從 `jsx-loader.html?component=...` 改成 `<script type="module" src="../assets/dist/<name>.js">`
4. （可選）寫 Vitest 測試在 `tests/portal/<Component>.test.tsx`

## 並存契約

TD-030 sweep 期間 jsx-loader.html 與 dist bundle **同時存在**：
- 已遷移工具 → dist bundle
- 未遷移工具 → jsx-loader.html
- 兩條路徑都 work，瀏覽器不中斷

最後 TD-030z PR 退役 jsx-loader.html。

## 相關
- TD-030 sub-issues: [#247-253](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/247)
- Project memory: `project_portal_vitest_choice.md`
- 現役 jsx-loader doc: [`docs/internal/jsx-multi-file-pattern.md`](../../docs/internal/jsx-multi-file-pattern.md)（TD-030z 後改寫為歷史記錄）
