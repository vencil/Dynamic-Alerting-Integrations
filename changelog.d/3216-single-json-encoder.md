---
section: Changed
topic: da-tools
issues: [3216, 3144, 3107]
created: 2026-10-30T12:52:44+00:00
---
- **`--json` 文件改由同一個出口寫出（da-tools；[#3216](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3216)）**：工具交回文件，由 `_lib_exitcodes.run` 以唯一編碼器寫出（兩格縮排、UTF-8、日期為 ISO 8601）。**Breaking**：文件含 NaN／Infinity（含 conf.d 的 `.inf`／`.nan`）、循環參照、印出後同名的鍵、過長整數或其他非 JSON 型別時，不再印出不合法 JSON 或卡住，改以 crash 碼 rc 2 離開，reason 為 `output_not_serializable`。已遷移 explain-route（未加引號的日期不再 crash，[#3144](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3144)，rc 2→0）與 dx、lint、ops 工具（單行輸出改縮排、非 ASCII 不再以 `\u` 跳脫、waveform 不排序鍵、scan-component-health 的 `Wrote:` 改印 stderr；analyze-gaps、batch-diagnose 的 `--json -o` 寫不進時 stdout 改印 `output_unwritable` envelope，後者帶 `total_tenants: 0`、`tenants: {}`；maintenance-scheduler 先 push metrics 再交出文件，push 擲出例外時為 `internal_error` envelope、silence 已建立；rc 皆仍為 2）。config-history 快照與 baseline CSV 改經共用寫入器設 0600（[#3107](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3107)），只收緊自身擁有、非經 symlink 的一般檔：他人的檔（非 root 寫入由 rc 2 改 rc 0）與經 symlink 的既有檔保留原 mode，FIFO 由卡住改 rc 2。
