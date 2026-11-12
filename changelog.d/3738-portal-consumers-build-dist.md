---
section: Changed
topic: portal
issues: [3738]
created: 2026-11-12T16:09:28+00:00
---
- **⚠️ breaking：portal bundle（`docs/assets/dist/`）移出 git，改由每個消費端自己從 source build（portal、da-portal image、GitHub Pages；[#3738](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3738)）**：da-portal image 新增 `portal-build` stage；Pages 部署、Smoke Tests、visual baseline 與本機 `make serve-docs`／`test-e2e`／`portal-bundle-budget` 都先 build，`make portal-build` 必要時先 `npm ci`（`portal-deps`）。`docs/assets/dist/` 加進 `.gitignore`，`dist-source-consistency-check` hook 與 CI 的 dist drift gate 刪除。⚠️ 遷移：拉到這版後工作樹沒有 `docs/assets/dist/`；直接開 `docs/` 的互動工具（static server、`mkdocs serve`、`npx playwright test`）前先 `make portal-build`。pull 前若有本機重 build 的 dist，先 `git restore docs/assets/dist`。改過 dist 的舊分支 rebase／merge 後跑 `git ls-files docs/assets/dist`，有輸出就 `git rm -r --cached docs/assets/dist`。自建流程若直接 serve／COPY repo 裡的 dist，改成先在 `tools/portal` 跑 `npm ci && npm run build`。
