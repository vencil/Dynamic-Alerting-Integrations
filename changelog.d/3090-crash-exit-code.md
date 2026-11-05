---
section: Changed
topic: da-tools
issues: [3090, 3094]
created: 2026-10-16T14:09:18+00:00
---
- **⚠️ 結束碼變更：Python 工具未捕捉的例外由 `1` 改為 `2`（[#3090](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3090)）**：先前工具 crash 時是直譯器預設的 `1`，與「有發現」的 `1` 分不開。現在 stderr 照樣印 `Traceback`，最後再加一行 `ERROR: unexpected internal error (<例外型別>): <訊息>`，結束碼 `2`（呼叫端／環境錯誤）。經 `da-tools` 執行一律如此；直接執行 `scripts/tools/` 下的腳本時，只限工具載入 `_lib_exitcodes` 之後才發生的 crash（缺 PyYAML 這類 import 期的錯誤仍是 `1`）；互動模式（`python -i`）不受影響；工具自己的 `sys.exit(N)` 與 Ctrl-C 不受影響。CI 若把 `1` 當成 crash 處理，請改看 `2` 與那一行 `ERROR:`。stdout 不補任何東西，帶 `--json` 也一樣（[#3558](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3558)）：先看結束碼。`tenant-verify` 同樣如此（見 [#3393](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3393)）。
