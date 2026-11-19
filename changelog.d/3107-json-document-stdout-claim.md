---
section: Changed
topic: da-tools
issues: [3107]
created: 2026-11-19T07:00:00+00:00
---
- **`--json` 文件也登記為標準輸出的擁有者（[#3107](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3107)）**：工具交回的 `--json` 文件印出前，先以「the --json document」登記標準輸出。同一次執行中已有 operator 輸出寫進標準輸出（`-` 或指向標準輸出的路徑）時，不再印第二份文件，stderr 印 `ERROR: cannot print the --json document: standard output is already used by <flag>`，結束碼 `2`（best-effort 的文件保留原結束碼，與標準輸出寫入失敗時相同）。以參數直接指名的單檔輸出在解析時就已擋下；目錄旗標（如 `waveform_compile --out`、`operator_generate --output-dir`）底下的某個檔若正是標準輸出的重導目標，以前 rc `0`、那個檔裡疊著兩份文件，現在改為 rc `2`。`assemble_config_dir` 在 `--json` 下改以同一名稱登記，`--manifest <檔> --json` 照舊印出文件、結束碼 `0`。
