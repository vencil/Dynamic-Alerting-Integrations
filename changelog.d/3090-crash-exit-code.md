---
section: Changed
topic: da-tools
issues: [3090, 3094]
created: 2026-10-16T14:09:18+00:00
---
- **⚠️ 結束碼變更：Python 工具未捕捉的例外由 `1` 改為 `2`（[#3090](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3090)）**：先前工具 crash 時是直譯器預設的 `1`，與「有發現」的 `1` 分不開。現在 stderr 照樣印 `Traceback`，最後再加一行 `ERROR: unexpected internal error (<例外型別>): <訊息>`，結束碼 `2`（呼叫端／環境錯誤）。經 `da-tools` 執行一律如此；直接執行 `scripts/tools/` 下的腳本時，只限工具載入 `_lib_exitcodes` 之後才發生的 crash（缺 PyYAML 這類 import 期的錯誤仍是 `1`）；互動模式（`python -i`）不受影響；工具自己的 `sys.exit(N)` 與 Ctrl-C 不受影響。唯一例外是 `tenant-verify`：它的 `2` 是「驗證不一致」，crash 維持 `1`。CI 若把 `1` 當成 crash 處理，請改看 `2` 與那一行 `ERROR:`。帶 `--json` 時 stdout 另有一份 envelope：`status: caller_error`、`reason: internal_error`，`error` 就是那一行 `ERROR:`；工具有早退報告時另帶其鍵（歸零）（[#3094](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3094)）；參數解析前就 crash、或工具已在 stdout 寫過東西之後才 crash 時沒有；`tenant-verify` 不在此列。
