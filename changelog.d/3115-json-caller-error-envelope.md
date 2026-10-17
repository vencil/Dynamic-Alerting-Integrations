---
section: Changed
topic: da-tools
issues: [3115]
created: 2026-10-17T07:49:30+00:00
---
- **`--json` 工具以結束碼 `2` 結束時，stdout 一律有一份 envelope（[#3115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3115)）**：找不到 da-guard、輸入 YAML 讀不了、輸出路徑寫不進去、observed-map 不可用等呼叫端錯誤，先前在 `diagnose`、`opa-evaluate`、`onboard`、`threshold-recommend` 等工具帶 `--json` 時 stdout 是空的。現在是 `{"status": "caller_error", "reason": <原因>, "error": <stderr 的 ERROR 行>}`，`reason` 依序為 `served_values_unavailable`／`yaml_file_unreadable`／`output_unwritable`／`observed_map_unavailable`，工具沒命名的是 `caller_error`；`config-diff`、`operator-generate` 自己接住例外的路徑是 `compare_failed`／`generate_failed`。結束碼不變；工具已自己印出文件時不再加印；argparse 拒絕的參數仍無 envelope；`threshold-recommend --export-patch`（未同時帶 `--generate-observed-map`）的 stdout 是 YAML 片段，也不加 envelope。
