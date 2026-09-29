---
section: Fixed
topic: confd-reader-consistency
issues: [2331, 2372]
created: 2026-09-28T16:47:51+00:00
---
- **`da_assembler.py --render-cr` 產出的 conf.d 值與檔名照 CR 原文寫出（tools；[#2331](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2331)、[#2372](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2372)）**：過去 CR 裡沒加引號的值會被改成 YAML 1.1 的型別再寫出：`010` 寫成 `8`、`0x1F` 寫成 `31`、`12:30` 寫成 `750`（`:30` 的 severity 跟著消失）、`2026-01-02T03:04:05Z` 寫成 `2026-01-02 03:04:05+00:00`，exporter 的生效值因此與 CR 原文不同。`metadata.name: 010` 則產出 `8.yaml`（`yes` 產出 `True.yaml`），tenant-api 以檔名找租戶，於是回 404。現在沒加引號的值照原文寫出，加了引號的值維持字串；輸出檔名、log 與檔頭註解使用 `metadata.name` 的原文；沒加引號、YAML 1.1 會讀成數字或布林的 name（如 `010`、`yes`）則依 [#2371](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2371) 回 rc 2，請加引號。⚠️ 升級或回滾時，持久的 `--config-dir` 要先清空或重建再 render：新舊版對同一 CR 的檔名可能不同（`2024-01-01T10:00:00Z.yaml` 對 `2024-01-01 10:00:00+00:00.yaml`、加引號後的 `010.yaml` 對 `8.yaml`），另一版留下的檔不會被刪，同一租戶於是被兩個檔宣告，exporter 拒收：冷啟動失敗，執行中則停止重載、沿用舊值，只在 log 留下記錄。name 因沒加引號被讀成數字或布林而被拒時，錯誤訊息會指名該刪的舊檔。
