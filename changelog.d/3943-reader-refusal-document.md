---
section: Changed
topic: tooling
issues: [3943]
created: 2026-11-15T06:00:00+00:00
---
- **⚠️ breaking：da-read 與 da-crdecode 拒收輸入時在 stdout 印被拒文件 `{"answer": "refused"}`，結束碼 2 而沒有它不再算拒收（[ADR-036](docs/adr/036-single-parser-effective-config.md) §3；[#3943](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3943)）**：拒收仍是結束碼 2，理由仍寫 stderr；`da-read prometheus-rules` 把 `answer` 放在 `files` 旁邊。文件只有固定的鍵與字串，不帶設定裡的文字。用法錯、打不開的檔（`prometheus-rules` 也是，不再列成該檔的 `problem`）、讀寫 stdin／stdout 失敗、Go 當機都不印它，Python 工具判為工具故障（產生器容器回 503 `tool_fault`，reason `unexpected_exit`；舊版程式的用法錯原本是 `stale`），不再當成輸入被拒。遷移：da-read、da-crdecode 與 Python 工具必須同版一起換上；舊版 binary 印不出這份文件，每次拒收都會被判為工具故障。
