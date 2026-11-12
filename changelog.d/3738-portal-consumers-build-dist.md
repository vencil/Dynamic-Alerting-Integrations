---
section: Changed
topic: portal
issues: [3738]
created: 2026-11-12T16:09:28+00:00
---
- **⚠️ breaking：portal bundle（`docs/assets/dist/`）移出 git，改由每個消費端自己從 source build（portal、da-portal image、GitHub Pages；[#3738](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3738)）**：da-portal image 新增 `portal-build` stage，build image 前不必先 `make portal-build`。Pages 部署在 `mkdocs build` 前先 build。Smoke Tests、visual baseline 與本機 `make serve-docs`／`test-e2e`／`portal-bundle-budget` 同樣先 build；`make portal-build` 在 `tools/portal/node_modules` 不存在或比 `package-lock.json` 舊時先 `npm ci`（`portal-deps`）。`docs/assets/dist/` 已加進 `.gitignore`，pre-commit 的 `dist-source-consistency-check` 與 CI 的 dist drift gate 刪除。⚠️ 遷移：拉到這個版本後 `docs/assets/dist/` 會從工作樹消失（既有 clone 也一樣），直接開 `docs/` 底下的互動工具（static server、`mkdocs serve`、直接 `npx playwright test`）前先 `make portal-build`；改 portal source 的 commit 只含 source；尚未合入、改過 dist 的分支 rebase 時撞到 dist 的 modify/delete 衝突，一律 `git rm -r --cached docs/assets/dist`。fork 或自建流程若直接 serve／COPY repo 裡的 `docs/assets/dist/`，要改成先在 `tools/portal` 跑 `npm ci && npm run build`。
