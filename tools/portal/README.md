---
title: "Portal Build & Test"
purpose: |
  ES modules build pipeline + Vitest unit-test harness for portal JSX tools.
  Foundation for the multi-PR sweep that migrates tools from `window.__X`
  registration to ESM `export {}`.
audience: [contributors, ai-agent]
lang: zh
---

# Portal Build & Test

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

## 結構（monorepo restructure 後）

```
tools/portal/
├── package.json        # esbuild + Vitest + RTL deps
├── manifest.json       # list of tool entries to bundle
├── build.mjs           # esbuild script — strips frontmatter, bundles per-entry
├── vitest.config.ts    # Vitest config — jsdom + frontmatter strip plugin
├── test-setup.ts       # global mocks for window.__t / React
├── tsconfig.json       # TS config covering src/ + tests/
├── src/                # ★ JSX source (was docs/interactive/ + docs/getting-started/)
│   ├── interactive/
│   │   └── tools/      # 43 portal tools + _common/ + subtree components
│   └── getting-started/
│       └── wizard.jsx
├── entries/            # *.entry.jsx — esbuild entry points
├── shims/              # build-time shims (lucide-react etc.)
└── tests/              # ★ Vitest specs (was tests/portal/)

docs/assets/dist/       # build output (per-tool ESM bundles) — written from build.mjs
docs/interactive/index.html  # portal hub page — STAYS in docs/ (it's a real docs page)
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

1. 在 `tools/portal/src/interactive/tools/<name>.jsx` 寫工具（用 ESM `export default`）
2. `tools/portal/manifest.json` 的 `entries` array 加入工具名（不含 `.jsx`）
3. 在 `tools/portal/entries/<name>.entry.jsx` 加 entry script（仿既有的）
4. （可選）寫 Vitest 測試在 `tools/portal/tests/<Component>.test.tsx`

## 並存契約

ESM sweep 期間 jsx-loader.html 與 dist bundle **同時存在**：
- 已遷移工具 → dist bundle
- 未遷移工具 → jsx-loader.html
- 兩條路徑都 work，瀏覽器不中斷

最終遷移完成後一次性 PR 退役 jsx-loader.html。

## 相關
- ESM sweep tracking: [issues #247-253](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/247)
- Project memory: `project_portal_vitest_choice.md`
- 現役 jsx-loader doc: [`docs/internal/jsx-multi-file-pattern.md`](../../docs/internal/jsx-multi-file-pattern.md)（jsx-loader 退役後改寫為歷史記錄）
