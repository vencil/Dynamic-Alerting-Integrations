---
section: Changed
topic: da-tools
issues: [3107]
created: 2026-11-19T05:00:00+00:00
---
- **舊寫入名稱退場，權限改由兩個輸出入口決定（[#3107](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3107)）**：`_lib_io`／`_lib_python` 移除 `write_text_secure`、`write_json_secure`、`write_text_or_die`、`write_json_or_die`，寫檔改經 `write_operator_output` 或 `write_repo_artifact`（分類見 `lint-policy.md`）。`inject_waveform --out` 改經 operator 入口：`-`、空值，以及指向本次執行標準輸出（檔案、管線或 socket）的路徑（如 `/dev/stdout`、`--out F > F`）在解析時結束碼 `2`（以前 `-` 寫出名為 `-` 的檔、空值不寫，都是 `0`；要名為 `-` 的檔傳 `./-`）；經 symlink 寫時目標保留原權限（以前改成 `0600`）。`test_sast.py` 的 open+chmod 啟發式與豁免表移除，連同只為它加的六處 `chmod 0644`（文件產生器寫回被追蹤的檔，現保留原權限）。`check_threshold_registry.py --regen` 暫存檔寫入失敗的訊息改為原始 `OSError`（結束碼不變）。
