---
section: Changed
topic: da-tools
issues: [3611, 3577, 3609, 3607, 3608]
created: 2026-11-05T23:25:00+00:00
---
- **⚠️ Breaking：「沒能檢查」改回 `2`，0/1/2 以外的結束碼要登記（[#3611](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3611)、[#3577](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3577)）**：「沒驗到」一律回 `2`，真正的發現仍是 `1`：缺 hadolint／helm／kube-linter／pint／node，或 `--render` 時缺 mmdc（帶不帶 `--ci` 都一樣，原本是警告加 `0`、pint 是 `1`，`CLI_CONTRACT_REQUIRE_NODE` 已刪）、引擎在但沒證明跑過（kube-linter 沒印報告、helm 沒輸出、pint 沒有 `entries=`、node 沒對每個檔回結果、mmdc 連已知正確的圖都渲染不出來，原本當成通過或發現）、image ref 的 registry 沒給答案（限流、5xx、連不上、逾時、讀不懂的回應）、輸入讀不到、`dc-run` 的 docker 或容器出狀況。要跳過請用 pre-commit 的 `SKIP=<id>`。`check_image_refs_resolve` 改直接問 registry 的 v2 API，不再需要 skopeo／docker，也不再看 `CI` 環境變數：registry 回 `MANIFEST_UNKNOWN`／`NAME_UNKNOWN`，或匿名讀取回 401 `UNAUTHORIZED`（repo 名打錯，或映像是私有的）才是 `1`；原本 skopeo 任何非零都算 `1`，本機沒裝 skopeo／docker 時回 `0`。`_lib_exitcodes.EXTENDED_EXIT_CODES` 是其他結束碼的唯一清單，`run()` 把未登記的碼改成 `2` 並印一行 ERROR；`da-tools guard` 的 3／4 與 `patch-config` 的 3–7 照舊。`dx_run --run` 與 `commit_helper` 傳出被包裝命令的結束碼。
