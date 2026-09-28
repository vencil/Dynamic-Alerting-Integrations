---
section: Added
topic: dx
issues: [1374]
created: 2026-09-28T18:30:00+08:00
---
- **`open-encoding-audit` 也管 subprocess 文字模式沒寫 `encoding=` 的呼叫（dx；[#1374](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1374)）**：`text=True`／`universal_newlines=True`／`errors=` 卻沒寫 `encoding=` 的 `subprocess` 呼叫，在 zh-TW Windows 上會以 cp950 解碼，失敗時呼叫端拿到的是 rc 0 加 `stdout None`。現有站點凍結在 `docs/internal/subprocess-encoding-baseline.json`（只減不增），新增的當場擋下；修掉之後用 `check_open_encoding.py --write-subprocess-baseline` 把帳本改小。該寫哪個編碼（由寫出的那一端決定）寫在工具的 docstring。
