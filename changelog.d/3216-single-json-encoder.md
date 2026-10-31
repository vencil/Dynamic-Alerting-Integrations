---
section: Changed
topic: da-tools
issues: [3216, 3144, 3107]
created: 2026-10-30T12:52:44+00:00
---
- **`--json` 文件改由同一個出口寫出（da-tools；[#3216](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3216)）**：工具交回文件，由 `_lib_exitcodes.run` 以唯一編碼器寫出（兩格縮排、UTF-8、日期為 ISO 8601、YAML 未加引號者照原文）。**Breaking**：文件含 NaN／Infinity（含 conf.d 的 `.inf`／`.nan`）、循環參照、印出後同名的鍵、過長整數或其他非 JSON 型別時，不再印出不合法 JSON 或卡住，改以 crash 碼 rc 2 離開，stderr 指出路徑，envelope reason 為 `output_not_serializable`。已遷移 explain-route（conf.d 未加引號的日期不再讓 `--json` crash，[#3144](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3144)，rc 2→0）與 dx、lint、部分 ops 工具（diagnose、check-alert、explain-keys 等單行輸出改縮排；waveform 報告不排序鍵；scan-component-health 的 `Wrote:` 改印 stderr；analyze-gaps、batch-diagnose 的 `--json -o` 寫不進時 stdout 由報告改印 `output_unwritable` envelope、rc 仍 2，後者帶骨架：`total_tenants: 0`、`tenants: {}`），其餘分批。config-history 快照與 baseline CSV 改經共用輸出寫入器寫出並設 0600（[#3107](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3107)），只收緊自身 euid 擁有、非經 symlink 的一般檔：他人的檔或經 symlink 的既有檔保留原 mode（非 root 寫他人可寫檔由 rc 2 改 rc 0），FIFO 由卡住改 rc 2。
