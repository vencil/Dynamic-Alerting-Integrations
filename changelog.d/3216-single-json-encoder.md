---
section: Changed
topic: da-tools
issues: [3216, 3144, 3107]
created: 2026-10-30T12:52:44+00:00
---
- **`--json` 文件改由同一個出口寫出（da-tools；[#3216](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3216)）**：工具交回文件，由 `_lib_exitcodes.run` 以唯一的編碼器寫出（兩格縮排、UTF-8、日期為 ISO 8601、YAML 未加引號的日期為原文）。**Breaking**：文件含 NaN／Infinity（含 conf.d 寫成 `.inf`／`.nan` 的值）、循環參照、印出後同名的鍵、過長的整數或其他非 JSON 型別時，不再印出不合法的 JSON（或卡住），改以 crash 結束碼 rc 2 離開，stderr 指出 JSON 路徑，`--json` 下的 envelope reason 為 `output_not_serializable`。已遷移 explain-route（conf.d 有未加引號的日期時 `--json` 不再 crash，[#3144](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3144)，rc 2→0）與 dx 工具（agent-output-metrics 改多行、waveform 報告不排序鍵、scan-component-health 的 `Wrote:` 改印 stderr），其餘分批遷移。config-history 的快照與 baseline 的 CSV 改由同一個輸出寫入器寫出並設 0600（[#3107](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3107)）：只收緊本身 euid 擁有、且非經 symlink 到達的一般檔。因此目標是他人擁有的檔時不再 chmod：非 root 寫入他人擁有的可寫檔由 rc 2（內容已寫入、chmod 失敗）改為 rc 0 且保留原 mode；root 寫入他人擁有的檔、或目標為指向既有檔的 symlink 時，也保留原 mode。目標為 FIFO 時由卡住改為 rc 2。
