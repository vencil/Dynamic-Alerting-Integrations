---
section: Fixed
topic: ci-pipeline
issues: [1949]
created: 2026-09-27T13:30:00+08:00
---
- **I-4 runbook smoke test 的工具換成與出貨同一代，amtool 語法檢查不再恆綠（ci；[#1949](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1949)）**：docs-ci 驗 troubleshooting-checklist 指令用的 promtool 從 2.x 升到與 k8s 出貨 Prometheus 同一個大版本（2.x 會放行 3.x 已移除的函式），並由 `tests/ops/test_docs_ci_promtool_major.py` 守住大版本一致；amtool 一併升版。amtool 那幾格原本只在輸出含特定字樣時判紅，而 amtool 的錯誤訊息對不上那些字樣，所以打錯 flag、值或子命令都照樣過——現在只有「連不到 Alertmanager／找不到 config 檔」這兩種預期中的執行期錯誤算通過，其餘一律紅。三個工具的下載都改為先比對 pin 住的 SHA-256。
