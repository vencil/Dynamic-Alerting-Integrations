---
section: Changed
topic: tooling
issues: [3943, 4015]
created: 2026-11-15T06:00:00+00:00
---
- **⚠️ breaking：da-read 與 da-crdecode 拒收輸入時在 stdout 印被拒文件 `{"answer": "refused"}`，結束碼 2 而沒有它不再算拒收（[ADR-036](docs/adr/036-single-parser-effective-config.md) §3；[#3943](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3943)）**：拒收仍是結束碼 2，理由仍寫 stderr；`da-read prometheus-rules` 把 `answer` 放在 `files` 旁邊。`answer` 是常數，不帶設定裡的文字；單一輸入的讀法 stdout 必須恰好是這份文件。用法錯（含 promql-selectors 不收的 series、stdin 的 JSON 外框不對）、打不開的檔（`prometheus-rules` 也是，不再列成該檔的 `problem`）、讀寫 stdin／stdout 失敗、da-read 自己的程式碼 panic（消費者函式庫在輸入上 panic 仍算拒收）都不印它，Python 工具判為工具故障（產生器容器回 503 `tool_fault`，reason `unexpected_exit`；舊版程式的用法錯原本是 `stale`），不再當成輸入被拒。Python 工具不會把自己讀不到的檔交給這兩支程式：`_lib_consumer_read` 先讀出檔案位元組，讀不到的就判該檔被拒（理由為 `the file cannot be opened: ` 加系統原因），逐檔回報的讀法只標那一個檔，其餘照讀，不會因一個讀不到的檔整批失敗；FIFO、socket、裝置等非一般檔不開啟就判被拒；開檔後以同一個 fd 再判斷並讀取，路徑中途換成 FIFO 也不會卡住。程式回結束碼 2 而沒有文件時 Python 重查：預讀後才消失的檔判被拒，都還讀得到才是工具故障（規則檔批次先把其餘檔重交一次）。遷移：da-read、da-crdecode 與 Python 工具必須同版一起換上；舊版 binary 印不出這份文件，每次拒收都會被判為工具故障。
