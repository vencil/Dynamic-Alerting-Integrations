# tools/portal — 動到這棵樹時的額外規則

你會讀到這份，是因為 diff 碰到了 `tools/portal/**`。⛔ **判別法是路徑，不是任務標題**——portal 檔常常是「非 portal 任務」的副產物（例如告警數變了，順手改 `rule-packs.js` 的一個數字），那時腦中的框架是「k8s / 規則」，於是整組 portal 規則都不會被想起來。

**不重複既有文件**：build / test 怎麼跑見 [`README.md`](README.md)；`_common/` 的消費方式見 [`src/interactive/tools/_common/README.md`](src/interactive/tools/_common/README.md)；`@visual` baseline 重生 SOP 見 [`tests/e2e/README.md`](../../tests/e2e/README.md)；lint gate 清單見 [`docs/internal/lint-policy.md`](../../docs/internal/lint-policy.md)。以下只列**那些文件沒有寫、而且踩過的**。

## dist 不進 git（#3738）

`docs/assets/dist/` 是 build 產物，**不 commit**（`.gitignore` 蓋住，`tests/lint/test_portal_dist_not_tracked.py` 守著）。每個消費端（Pages 部署、da-portal image、Smoke／visual baseline、本機 `serve-docs`／`test-e2e`）都在用之前自己 build；清單與守衛見 [`README.md`](README.md)「誰消費」一節。

⇒ 改 portal source 的 commit **只含 source**。不要 `git add -f` dist 回來，也不必為了「dist 要跟上」而重 build——那件事已經沒有 gate 在比對。要在本機看結果才跑 `make portal-build`（`make serve-docs`／`test-e2e*` 會自己跑）。

⛔ 舊分支（#3738 之前開、改過 dist）rebase／merge 到之後的 main：撞上 dist 的 modify/delete 衝突時**不要解衝突內容**；而只**新增** chunk 的 commit 不會衝突、會把 dist 悄悄帶回 index。所以不論有無衝突，rebase／merge 完都跑 `git ls-files docs/assets/dist`，有輸出就 `git rm -r --cached docs/assets/dist`。本機重 build 過、還沒 commit 的 dist 會讓 `git pull` 中止：pull 前先 `git restore docs/assets/dist`（本機產物，可丟）。

`Portal Tests`（Vitest）與 `Smoke Tests (Chromium)` 每支 PR 都跑（[#3756](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3756) 起沒有 path filter），leg 被 skip 時 aggregate 判紅。要看某顆 SHA 上具名的 job：

```bash
gh api repos/vencil/Dynamic-Alerting-Integrations/commits/<sha>/check-runs --jq '.check_runs[] | select(.name|test("Portal")) | "\(.name) \(.status) \(.conclusion)"'
```

## 新增 portal lint

⛔ **只放進 Makefile 的 `lint-portal` target 等於沒上膛——CI 不呼叫它。** CI 的 `Lint` job 跑的是**逐條列出的** `pre-commit run <id> --all-files`（`make lint-docs` 在另一個 job `Lint Documentation`）。而 `check_orphan_lint` 把「Makefile 有引用」算成已接線，所以它會過——**過了不等於有在擋**。要真的生效：`.pre-commit-config.yaml` 加 `- id:`（`pass_filenames: false`、`files:` 寫它實際掃的路徑），**並且**在 `ci.yml` 的 Lint job 補一行。

## 新增 portal 要消費的資料

⛔ **不要加進 `platform-data.json` 的 `rulePacks[*]`。** `rule-packs-fallback-drift.test.ts` 的 `carried()` 是**明列欄位的白名單投影**，不是全欄比對——加在 per-pack 底下的新欄位兩側都被丟掉，drift gate 完全不會比對它（變異實測：零轉紅），而離線 fallback 仍然需要手抄它 ⇒ 你會得到一份無人看守的手抄。

正解：放 `platform-data.json` **頂層** + 自建 drift gate，形狀照抄 `tools/portal/tests/images-fallback-drift.test.ts`（從 JSON 讀期望值 → `vi.resetModules()` → `delete window.__PLATFORM_DATA` → `await import(MOD)` 重新求值）。

⚠️ 離線 fallback 寫成 `|| Object.keys(...)` 這種**推導**是對的；寫成 `|| {}` 是**離線直接還原成 bug**。兩者不是同一種形狀。

## portal → portal 的連結

⛔ 用 `../assets/jsx-loader.html?component=<key>`，**不要**寫 `<tool>.html`。沒有 per-tool 的 `.html` 檔，而 da-portal 的 nginx `try_files ... /interactive/index.html` 會把不存在的路徑**靜默**退回 Hub 首頁——看起來是能用的連結，實際上到不了那個工具。證明它有效的是 Playwright 的點擊路徑，不是截圖。

## 設計階段

評估「該不該升級某個 portal entry 的 UX」之前，**預設它是 demo / onboarding**。要主張它是 production surface，三項要全過：資料來源是真的（不是 `MOCK_*` / `EXAMPLE_*` / 寫死陣列）、frontmatter `audience:` 以 `platform-engineer` / `sre` 為主（實際在用的值只有這幾個，沒有 `operator`）、frontmatter `related:` 以 production 工具為主。任一項不過就重新 scope 成「改善 demo 完成度」。

⚠️ 反過來也不要一竿子打翻：`tenant-manager` 接真的 tenant-api，是真的 production 工具。**逐個開檔判斷，不要從一個 entry 推論整棵樹。**
