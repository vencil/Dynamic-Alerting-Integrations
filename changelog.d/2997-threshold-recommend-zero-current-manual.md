---
section: Fixed
topic: threshold-governance
issues: [2997]
created: 2026-10-16T00:21:12+00:00
---
- **`threshold-recommend`／`threshold-govern`：現值為 0 的上界門檻不再被自動放寬（[#2997](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2997)）**：門檻 0 代表「任何大於 0 的值都告警」，常是刻意設定（如 `kafka_under_replicated_partitions`）。先前只要指標有一點活動，就會建議改成 P95，而變動寫死為 100%，永遠通過 `--min-delta-pct`，govern 照開 PR。現在這類 key 不給建議值、`delta_pct` 為空，標成人工審查：reason 寫明現值為 0 並附觀測到的 P99（依現值精度向上取整）與最大值；`--export-patch` 以 `(manual)` 註解列出，govern 列進人工審查段並計數，不開 PR。`--apply` 不再為它寫入，結束碼因此有兩種情況會不同：可寫入的只有這類 key 且寫入失敗時由 1 變 0；它的寫入原本是整輪唯一的成功、其他租戶寫入都失敗時由 0 變 1；`--max-prs` 名額改由其他租戶使用（與 plan 無關的健康訊號另見 [#3054](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3054)）。資料全為 0 的照舊不給建議。
