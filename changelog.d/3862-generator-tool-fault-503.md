---
section: Changed
topic: tenant-api
issues: [3862]
created: 2026-11-14T13:30:00+00:00
---
- **⚠️ breaking：產生器容器判定時有外部程式沒跑成，那筆寫入回 503 `tool_fault`（[ADR-036](docs/adr/036-single-parser-effective-config.md) §3；[#3862](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3862)）**：一次判定裡 da-guard、da-read 或 da-crdecode 任一支找不到、無法執行、逾時、被訊號結束（記憶體上限下 Go 執行期因記憶體不足自己結束的除外，含啟動時結束碼 2：回 200 `rejected` `memory`，不改變既有的 ready 狀態）、結束碼或輸出不在它的契約內、或比工具舊，服務回 503 `VALIDATOR_UNAVAILABLE`、`reason: tool_fault`，`message` 點名程式、子指令與原因（不帶路徑與 stderr）。以前會被當成內容的錯（200 `findings`）或照樣通過（200 `pass`）。判定前的逐支探測刪除：503 reason `da_read_missing`、`da_guard_missing` 移除；`GET /v1/status` 的 `da_read_available`、`da_guard_available` 換成 `tool_fault`（null，或最近一次判定的 `{binary, subcommand, reason}` 清單），`ready` 在最近一次判定有工具故障時為 false，只有下一次跑完全程的判定才恢復（`rejected` 與 400 不清除）。命令列不變。遷移：看 `reason` 為 `da_read_missing`／`da_guard_missing` 的告警與查詢改看 `tool_fault`（`da_generator_unavailable_total{reason="tool_fault"}`）；讀 `/v1/status` 的 `da_read_available`／`da_guard_available` 的改讀 `tool_fault`。tenant-api 仍認得送 `da_guard_available` 的舊版產生器映像。
