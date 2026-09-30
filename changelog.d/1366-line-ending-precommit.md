---
section: Changed
topic: dx
issues: [1366]
created: 2026-09-30T22:20:00+08:00
---
- **忘了 `newline=` 改在 commit 當下擋，不必等 push 後的 CI（dx；[#1366](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1366)）**：行尾政策的判定併進 pre-commit `open-encoding-audit`（`check_open_encoding.py` 新增 `--strict-line-ending` 與可重複的 `--line-ending-root`），與 `encoding=` 共用一次 AST 掃描。範圍仍是 `scripts`／`components`／`helm`、不含 `tests/`；`# line-ending: ignore` 語意不變。同一個 `open()` 同時缺 `encoding=` 與 `newline=` 時只印一行、點名兩者。`tests/dx/test_line_ending_policy.py` 不再自帶判定器，改對 hook 的實作跑正反例，並保留一支全樹掃描當 CI 上的執行點。
