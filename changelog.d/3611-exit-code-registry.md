---
section: Changed
topic: da-tools
issues: [3611, 3577, 3609]
created: 2026-11-05T23:25:00+00:00
---
- **⚠️ Breaking：「沒能檢查」改回 `2`，0/1/2 以外的結束碼要登記（[#3611](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3611)、[#3577](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3577)）**：會讓檢查失敗的那些「沒驗到」——`--ci` 下缺 hadolint／helm／kube-linter、CI 上缺 skopeo、輸入讀不到、resolver 逾時、`dc-run` 的 docker 或容器出狀況——原本回 `3`／`4`／`1`／`127`，現在是 `2`；真正的發現仍是 `1`。本機不帶 `--ci` 缺引擎仍是警告加 `0`。`_lib_exitcodes.EXTENDED_EXIT_CODES` 是其他結束碼的唯一清單，`run()` 把未登記的碼改成 `2` 並印一行 ERROR；`da-tools guard` 的 3／4 與 `patch-config` 的 3–7 照舊。`dx_run --run` 與 `commit_helper` 傳出被包裝命令的結束碼。
