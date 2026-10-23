---
section: Fixed
topic: da-tools
issues: [3237]
created: 2026-10-22T15:00:00+00:00
---
- **`python -i` 下的事後除錯拿得到工具原本的例外（[#3237](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3237)）**：經 `_lib_exitcodes.run(main)` 進入的工具，在互動式 session（`python -i <工具>.py`、在終端機上設 `PYTHONINSPECT`、REPL）裡遇到例外時，先前由 `run()` 自己接住並轉成 `SystemExit(2)`，`pdb.pm()`／`sys.last_value` 只看得到 `SystemExit`；現在比照模組的 excepthook，互動式 session 下把原例外交給預設 hook。非互動式執行（含所有正式用法）的結束碼與輸出不變。
