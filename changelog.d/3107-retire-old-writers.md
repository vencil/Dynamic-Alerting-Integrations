---
section: Changed
topic: da-tools
issues: [3107]
created: 2026-11-19T05:00:00+00:00
---
- **舊寫入名稱退場，寫入權限改由兩個輸出入口負責（[#3107](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3107)）**：`_lib_io`／`_lib_python` 不再提供 `write_text_secure`、`write_json_secure`、`write_text_or_die`、`write_json_or_die`；寫檔一律經 `write_operator_output`（operator 輸出）或 `write_repo_artifact`（artifact），分類規則見 `docs/internal/lint-policy.md`。`inject_waveform --out` 改經 operator 入口：`--out -` 與空值在解析時結束碼 `2`（以前 `-` 會寫出名為 `-` 的檔，空值被當成沒給），要名為 `-` 的檔傳 `./-`；寫入結果不變（就地寫、自己擁有的一般檔 `0600`）。`test_sast.py` 的 open+chmod 啟發式與豁免表移除。
