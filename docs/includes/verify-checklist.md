**標準驗證清單：**

- [ ] threshold-exporter `/ready` 回應 200
- [ ] Prometheus targets 顯示 exporter UP
- [ ] `da-tools diagnose --tenant <NAME>` 全項 PASS
- [ ] Recording rules 正確產出 `tenant:*` 指標
- [ ] Alert rules 狀態正常（無 pending/firing 誤報）
