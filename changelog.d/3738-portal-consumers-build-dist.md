---
section: Changed
topic: portal
issues: [3738]
created: 2026-11-12T16:09:28+00:00
---
- **portal bundle 改由每個消費端自己從 source build（da-portal image、GitHub Pages；[#3738](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3738)）**：da-portal image 新增 `portal-build` stage（`node:24-alpine` 釘 digest、`--platform=$BUILDPLATFORM`），最終 stage 改從該 stage 取 `/assets/dist/`，不再 COPY repo 裡 commit 的 `docs/assets/dist/`；build image 前不必先 `make portal-build`。Pages 部署在 `mkdocs build` 前先 build，且只改 `tools/portal/**` 的 push 也會觸發部署。Smoke Tests、visual baseline 與本機 `make serve-docs`／`test-e2e`／`portal-bundle-budget` 同樣先 build；`make portal-build` 在 `tools/portal/node_modules` 不存在或比 `package-lock.json` 舊時會先 `npm ci`（新 target `portal-deps`）。`docs/assets/dist/` 這次仍留在 git，下一個變更才移出。
