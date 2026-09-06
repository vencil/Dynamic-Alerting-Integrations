# tools/portal — 動到這棵樹時的額外規則

你會讀到這份，是因為 diff 碰到了 `tools/portal/**`。⛔ **判別法是路徑，不是任務標題**——portal 檔常常是「非 portal 任務」的副產物（例如告警數變了，順手改 `rule-packs.js` 的一個數字），那時腦中的框架是「k8s / 規則」，於是整組 portal 規則都不會被想起來。

**不重複既有文件**：build / test 怎麼跑見 [`README.md`](README.md)；`_common/` 的消費方式與 dist gate 全表見 [`src/interactive/tools/_common/README.md`](src/interactive/tools/_common/README.md)；`@visual` baseline 重生 SOP 見 [`tests/e2e/README.md`](../../tests/e2e/README.md)；lint gate 清單見 [`docs/internal/lint-policy.md`](../../docs/internal/lint-policy.md)。以下只列**那些文件沒有寫、而且踩過的**。

## commit 階段

⛔ **`git commit --amend` 補 dist 一定失敗，要用 `git reset --soft HEAD~1`。**
`dist-source-consistency-check` 要求「有 dist 進 index，就必須有對應 source 也在 index」。amend 時 source 已經在 HEAD、不在 index，於是 hook 看到的是一顆 dist-only commit。正解是 soft-reset 後把 source 與 dist 一起重新 stage。（`BYPASS_DIST_CHECK=1` 存在但不要用。）

## 什麼時候真的要重建 dist

`sourcemap: 'linked'`，所以 **`.js.map` 對行號位移敏感、對內容不敏感**：

| 改動 | 要不要重建 |
|---|---|
| 新增/刪除任何一行（含註解行） | **要**——`.js.map` 的 `mappings` 會重映射，即使壓縮後的 `.js` 完全相同 |
| 同一行內替換 frontmatter 值（行數不變） | **不用**——frontmatter 在進 esbuild 前就被 `stripFrontmatterPlugin` 剝掉，dist 位元相同 |
| 改 `_common/` 底下任何東西 | **要**，而且會連動：它在 shared chunk 裡，多個 entry 的 `.js` 會一起被重指向、舊 chunk 刪除新 chunk 新增。**那是預期，不是 drift**，全部一起 commit |

⛔ **不要用「CI 全綠」推論 portal 那條 leg 有跑過**：path filter 沒命中時它是 **skipped 不是 failure**，畫面一樣綠。要查那顆 SHA 上具名的 job：

```bash
gh api repos/vencil/Dynamic-Alerting-Integrations/commits/<sha>/check-runs --jq '.check_runs[] | select(.name|test("Portal")) | "\(.name) \(.status) \(.conclusion)"'
```

## 新增 portal lint

⛔ **只放進 Makefile 的 `lint-portal` target 等於沒上膛——CI 不呼叫它。** CI 的 Lint job 跑的是 `make lint-docs` 加上逐條列出的 `pre-commit run <id> --all-files`。而 `check_orphan_lint` 把「Makefile 有引用」算成已接線，所以它會過——**過了不等於有在擋**。要真的生效：`.pre-commit-config.yaml` 加 `- id:`（`pass_filenames: false`、`files:` 寫它實際掃的路徑），**並且**在 `ci.yml` 的 Lint job 補一行。

## 新增 portal 要消費的資料

⛔ **不要加進 `platform-data.json` 的 `rulePacks[*]`。** `rule-packs-fallback-drift.test.ts` 的 `carried()` 是**明列欄位的白名單投影**，不是全欄比對——加在 per-pack 底下的新欄位兩側都被丟掉，drift gate 完全不會比對它（變異實測：零轉紅），而離線 fallback 仍然需要手抄它 ⇒ 你會得到一份無人看守的手抄。

正解：放 `platform-data.json` **頂層** + 自建 drift gate，形狀照抄 `tools/portal/tests/images-fallback-drift.test.ts`（從 JSON 讀期望值 → `vi.resetModules()` → `delete window.__PLATFORM_DATA` → `await import(MOD)` 重新求值）。

⚠️ 離線 fallback 寫成 `|| Object.keys(...)` 這種**推導**是對的；寫成 `|| {}` 是**離線直接還原成 bug**。兩者不是同一種形狀。

## portal → portal 的連結

⛔ 用 `../assets/jsx-loader.html?component=<key>`，**不要**寫 `<tool>.html`。沒有 per-tool 的 `.html` 檔，而 da-portal 的 nginx `try_files ... /interactive/index.html` 會把不存在的路徑**靜默**退回 Hub 首頁——看起來是能用的連結，實際上到不了那個工具。證明它有效的是 Playwright 的點擊路徑，不是截圖。

## 設計階段

評估「該不該升級某個 portal entry 的 UX」之前，**預設它是 demo / onboarding**。要主張它是 production surface，三項要全過：資料來源是真的（不是 `MOCK_*` / `EXAMPLE_*` / 寫死陣列）、frontmatter `audience:` 以 `sre` / `operator` / `platform-engineer` 為主、frontmatter `related:` 以 production 工具為主。任一項不過就重新 scope 成「改善 demo 完成度」。

⚠️ 反過來也不要一竿子打翻：`tenant-manager` 接真的 tenant-api，是真的 production 工具。**逐個開檔判斷，不要從一個 entry 推論整棵樹。**
