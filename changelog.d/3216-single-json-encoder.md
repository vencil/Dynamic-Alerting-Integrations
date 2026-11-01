---
section: Changed
topic: da-tools
issues: [3216, 3144, 3107]
created: 2026-10-30T12:52:44+00:00
---
- **`--json` 文件與工具寫出的 JSON 改用同一個編碼器（da-tools；[#3216](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3216)）**：`--json` 文件由 `_lib_exitcodes.run` 寫出（兩格縮排、UTF-8、日期為 ISO 8601）；config-history、state-reconcile、assemble `--manifest`、migrate-to-operator 寫的 JSON 也用它，`format_json_report` 已刪除。**Breaking**：含 NaN／Infinity（含 conf.d 的 `.inf`／`.nan`）、循環參照、印出後同名的鍵、過長整數或其他非 JSON 型別時，改以 rc 2 離開（reason `output_not_serializable`）；config-history 的 history.json 含 NaN 時 rc 0→2。explain-route 未加引號的日期不再 crash（[#3144](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3144)，rc 2→0）。單行輸出改縮排、非 ASCII 不再以 `\u` 跳脫、waveform 不排序鍵、scan-component-health 的 `Wrote:` 改印 stderr；analyze-gaps、batch-diagnose 的 `--json -o` 寫不進時 stdout 改印 `output_unwritable` envelope；maintenance-scheduler 先 push 再交出文件（rc 皆仍為 2）。config-history 快照與 baseline CSV 改經共用寫入器設 0600（[#3107](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3107)），他人的檔與經 symlink 的既有檔保留原 mode（非 root 由 rc 2 改 0），FIFO 由卡住改 rc 2。
