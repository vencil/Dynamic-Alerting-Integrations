---
section: Fixed
topic: dev-workflow
issues: [2558]
created: 2026-10-01T12:00:00+08:00
---
- **五支工具在 Windows 上印出的相對路徑改用 `/`（internal、dx；[#2558](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2558)）**：`generate_nav.py` 的 nav 草稿以前印出 `adr\001-x.md`，不能直接貼進 MkDocs nav，排除規則也是拿帶 `\` 的字串去比對；`check_path_metadata_consistency.py` 的 finding、`compile_custom_alerts` 錯誤項目的來源檔、`generate_tool_map.py` 的訊息，以及 `backtest_threshold.py --git-diff` 讀不了檔時點名的路徑同樣帶 `\`。現在五支在每個平台印出相同的文字；Linux 上的輸出不變。
