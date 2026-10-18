---
section: Fixed
topic: ci
issues: [3177]
created: 2026-10-18T11:04:01+00:00
---
- **nightly race 回報認得 package 逾時，不再把它說成 test-isolation 失敗（[#3177](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3177)）**：`go test -timeout` 到期時沒有任何測試送出 `fail`，先前 `parse_go_test_json` 只得到「無法歸屬」，追蹤 issue 卻仍寫「repeat-run / test-isolation failure」並要人二分 `-count`。現在 fragment 帶 `timed_out`、`timeout_packages` 與 `timeout_running_tests`（逾時當下在跑的測試），issue 判定為 `package timeout`，列出逾時的 package 與在跑的測試，Triage 改為比對耗時與 `-timeout`。無法歸屬時改寫「原因未歸屬」，isolation 的敘述與二分 `-count` 只在真的有失敗測試時出現。
