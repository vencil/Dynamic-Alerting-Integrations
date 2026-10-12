---
section: Fixed
topic: tenant-api
issues: [2913, 2486]
created: 2026-10-11T16:54:08+00:00
---
- **非同步批次的工作 panic 不再拖垮 tenant-api（tenant-api；[#2913](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2913)）**：`?async=true` 批次由 worker 執行，先前工作中任何 panic 都會讓整個 tenant-api 結束。現在 worker 攔下 panic：該 task 標為 `failed`，`error` 只說明工作意外中止、不帶 panic 內容或堆疊（兩者依 task id 寫進 log），worker 繼續處理後續 task。新增 counter `tenant_api_async_task_panics_total`，應維持 0；15 分鐘內有增加就觸發 warning 平台告警 `TenantApiAsyncTaskPanicked`。
