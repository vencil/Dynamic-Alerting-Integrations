---
section: Added
topic: dx
issues: [1374]
created: 2026-09-28T18:30:00+08:00
---
- **`open-encoding-audit` 也管 subprocess 文字模式沒寫 `encoding=` 的呼叫（dx；[#1374](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1374)）**：`text=True`／`universal_newlines=True`／`errors=` 卻沒寫 `encoding=` 的 `subprocess` 呼叫，在 zh-TW Windows 上會以 cp950 解碼，失敗時呼叫端拿到的是 rc 0 加 `stdout None`。repo 內既有的站點都已寫明編碼，帳本 `docs/internal/subprocess-encoding-baseline.json` 是空的，新增的站點當場擋下。該寫哪個編碼（由寫出的那一端決定；`-z` 列出的檔名用 `surrogateescape`）寫在工具的 docstring。
