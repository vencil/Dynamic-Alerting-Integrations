---
section: Security
topic: tenant-api
issues: [2811]
created: 2026-10-20T12:45:06+00:00
---
- **⚠️ breaking：`GET /ready` 的回應不再帶 config 目錄、socket 路徑與錯誤原文（tenant-api；[#2811](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2811)）**：`/ready` 不經身分驗證，先前 503 會回 `config_dir`、`error`（stat 錯誤原文）與 `human_socket`，200 也帶 `config_dir`。現在 body 只有 `status`，失敗時加一個固定的 `reason`：`config_dir_unavailable`、`config_dir_not_a_directory`、`human_socket_down`；200 只回 `{"status":"ready"}`。路徑與錯誤原文改寫進 server log 的 `readiness check failed`（WARN，附 `request_id`）。status code 不變，kubelet 探針不受影響；讀 body 裡 `config_dir`／`error`／`human_socket` 的腳本要改看 log。
