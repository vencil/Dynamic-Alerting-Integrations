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
│   │   └── tools/      # portal tools + _common/ + subtree components
│   └── getting-started/
│       └── wizard.jsx
├── entries/            # *.entry.jsx — esbuild entry points
├── shims/              # build-time shims (lucide-react etc.)
└── tests/              # ★ Vitest specs (was tests/portal/)

docs/assets/dist/       # build output (per-tool ESM bundles) — written from build.mjs; gitignored (#3738)
docs/interactive/index.html  # portal hub page — STAYS in docs/ (it's a real docs page)
```

## 誰消費 `docs/assets/dist/`：各自 build

dist 是 build 產物、不在 git 裡，每個消費端都在用之前自己從這棵樹 build（[#3738](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3738)）：

| 消費端 | 在哪裡 build |
|---|---|
| GitHub Pages（`mkdocs-deploy.yaml`） | `mkdocs build` 前 `npm ci` + `npm run build`；`on.push.paths` 含 `tools/portal/**` |
| da-portal image（`components/da-portal/Dockerfile`） | `portal-build` stage（`node` 映像、`--platform=$BUILDPLATFORM`、釘 digest），最終 stage `COPY --from=portal-build` |
| Smoke Tests（`ci.yml` `smoke-tests-run`）、`visual-baseline.yaml` | 起 server 之前 build |
| 本機 `make serve-docs` / `test-e2e*` / `portal-bundle-budget` | 依賴 `portal-build` target |

守衛：`tests/ops/test_portal_dist_consumers_build.py`。`lint_tool_consistency.py` 判斷「某個 bundle 存不存在」讀的是 `manifest.json` + `entries/`，不是 dist 目錄。

dist **不 commit 進 git**（`.gitignore` 蓋住；守衛 `tests/lint/test_portal_dist_not_tracked.py`）。拉到 #3738 之後工作樹就沒有 `docs/assets/dist/`（既有 clone checkout 過去也會被刪；本機重 build 過、還沒 commit 的 dist 會讓 `git pull` 中止，先 `git restore docs/assets/dist`）。舊分支 rebase／merge 完跑 `git ls-files docs/assets/dist`，有輸出就 `git rm -r --cached docs/assets/dist`（只新增 chunk 的 commit 不會衝突、會把它悄悄帶回）。要在本機直接開 `docs/` 底下的互動工具，先 `make portal-build`（`make serve-docs`／`test-e2e*` 會自己跑）。CI 的 Portal Tests 仍跑 `npm run build`＋bundle size budget＋Vitest，量的是當次 build 的產物。

## 為什麼 esbuild

- **Zero dev-server complexity**：`python -m http.server` 已經夠用，不需要 Vite dev server overhead
- **Deterministic output paths**：`docs/assets/dist/<entry>.js`，CI 重現性
- **One file**：整個 build 流程都在 `build.mjs` 這一支裡，讀完即可掌握
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
