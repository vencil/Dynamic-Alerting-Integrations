---
section: Fixed
topic: da-tools
issues: [3822]
created: 2026-11-21T12:40:51+00:00
---
- **互動提示讀不到 stdin 時改為具名的呼叫端錯誤，不再是 crash（da-tools；[#3822](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3822)）**：`scaffold-tenant`、`init` 與 `migrate-rule --interactive` 在 CI、cron、`< /dev/null` 這類沒有終端機的環境下會以 `unexpected internal error (EOFError)` 加 traceback 結束，看不出該改用哪個旗標。現在一律 rc 2、stderr 一行 `ERROR:`，點名不經提示的跑法：`scaffold-tenant` 與 `init` 指 `--non-interactive`（及各自必填的 `--tenant`／`--db`、`--tenants`），`migrate-rule` 指「不帶 `--interactive` 重跑」。所有提示改經共用的 `_lib_prompt.prompt_line()` 讀取，新的 AST 棘輪 `tests/shared/test_prompt_line_boundary.py` 擋下 `scripts/` 底下任何裸 `input()`。
