---
section: Fixed
topic: ci
issues: [2643]
created: 2026-10-03T06:38:04+00:00
---
- **`subprocess-timeout-audit` 在 CI 有了執行點（ci；[#2643](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2643)）**：這支 FATAL hook 只登記在 `.pre-commit-config.yaml`，CI Lint job 逐名跑 hook 的清單裡沒有它，於是 `subprocess` 呼叫漏掉 `timeout=` 只有本機 hook 擋得到；它的 pytest 只測腳本函式，拿掉一處 `timeout=` 仍然全綠。Lint job 現在跑 `pre-commit run subprocess-timeout-audit --all-files`，並以解析 YAML 的斷言釘住那一行、確認 hook 沒被篩離它的檔案，gate liveness 測試也加了這支 hook 的違規／合法案例。CI 接線的判定抽成共用 helper，與 `open-encoding-audit` 共用一份。
