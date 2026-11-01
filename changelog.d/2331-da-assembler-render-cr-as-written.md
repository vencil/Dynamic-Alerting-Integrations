---
section: Fixed
topic: confd-reader-consistency
issues: [2331, 2372]
created: 2026-09-28T16:47:51+00:00
---
- **`da_assembler.py --render-cr` 產出的 conf.d 值與檔名不再經 PyYAML 改型別（tools；[#2331](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2331)、[#2372](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2372)）**：過去 CR 裡沒加引號的值會被改成 YAML 1.1 的型別再寫出：`12:30` 寫成 `750`（`:30` 的 severity 跟著消失）、`2026-01-02T03:04:05Z` 寫成 `2026-01-02 03:04:05+00:00`。`metadata.name: 010` 則產出 `8.yaml`（`yes` 產出 `True.yaml`），tenant-api 以檔名找租戶，於是回 404。現在值依 Kubernetes client 的解碼寫出（#2476；`12:30` 與日期時間維持文字）；輸出檔名、log 與檔頭註解使用 `metadata.name` 的原文；沒加引號、Kubernetes 會讀成數字或布林的 name（如 `010`、`yes`）則依 [#2371](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2371) 回 rc 2，請加引號（加了仍不合 DNS-1123 者如 `TRUE` 須改名）。⚠️ 升級或回滾時，持久的 `--config-dir` 要先清空或重建再 render：舊版對某些 name 寫出的檔名與現行不同（`010` 寫成 `8.yaml`，現行加引號後是 `010.yaml`），舊檔不會被刪，同一租戶於是被兩個檔宣告，exporter 拒收：冷啟動失敗，執行中則停止重載、沿用舊值，只在 log 留下記錄。name 因被讀成數字、布林、null 或日期時間而被拒時，若 `--config-dir` 裡有舊版可能為它寫的檔（如 `True.yaml`），錯誤訊息會指名該檔並提醒先確認不是另一個 CR 的輸出再刪；檔頭屬別的 CR 則說勿刪。
