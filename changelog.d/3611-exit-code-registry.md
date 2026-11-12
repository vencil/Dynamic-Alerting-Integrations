---
section: Changed
topic: da-tools
issues: [3611, 3577, 3609, 3607, 3608]
created: 2026-11-05T23:25:00+00:00
---
- **⚠️ Breaking：「沒能檢查」改回 `2`，0/1/2 以外的結束碼要登記（[#3611](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3611)、[#3577](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3577)）**：「沒驗到」一律 `2`，發現仍是 `1`：缺 hadolint／helm／kube-linter／pint／node，或 `--render` 缺 mmdc（不論 `--ci`；`CLI_CONTRACT_REQUIRE_NODE` 已刪）；引擎在但沒證明跑過（原本當成通過或發現）；輸入讀不到；`dc-run` 的 docker 出狀況。要跳過請用 pre-commit 的 `SKIP=<id>`。`check_image_refs_resolve` 改問 registry v2 API，不需 skopeo／docker、不看 `CI`：`MANIFEST_UNKNOWN`／`NAME_UNKNOWN` 或匿名 401 才是 `1`，限流、5xx、連不上是 `2`。recipe-preview 的 would-fire 改由兩組斷言相反的 `promtool test rules --junit` 報告判定，promtool 沒能評估一律 error；`scenario.value` 只收有限的 JSON 數字，數字字串（如 `"1500"`）改判 error。`_lib_exitcodes.EXTENDED_EXIT_CODES` 是其他結束碼的唯一清單，`run()` 把未登記的碼改成 `2`；`da-tools guard` 的 3／4、`patch-config` 的 3–7 照舊，`dx_run --run`、`commit_helper` 傳出被包裝命令的碼。
