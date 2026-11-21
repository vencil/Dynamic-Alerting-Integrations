---
section: Changed
topic: da-tools
issues: [3981]
created: 2026-11-21T02:40:00+00:00
---
- **⚠️ 四支文件產生器改經生成物入口寫檔，`--safe` 旗標移除（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：`generate_tool_map.py`、`generate_doc_map.py` 的 `--generate` 每次都原子取代輸出，只有就地寫入退路例外（輸出檔有多個 hard link、屬於別人、帶 xattr，或目錄不可寫；以前要帶 `--safe` 才是，預設是就地寫入）；`--safe` 不再接受，帶了會以結束碼 `2` 結束。`generate_rule_pack_stats.py`、`generate_byo_rulepack_table.py` 同樣改經 `write_repo_artifact`。行為差異：寫到一半失敗時舊檔保留（原子取代的情況；走就地寫入退路時不保證，見 [#4106](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4106)；以前兩支 map 產生器只留下前半段）；既有檔的執行位保留（以前一律改成 `0644`）；輸出路徑是 FIFO 時結束碼 `2`（以前卡住；四支都在讀取目標之前先檢查）；把 stdout 重導到輸出檔時結束碼 `2`（兩支 map 產生器以前是 `0`）；輸出檔有多個 hard link 時就地寫入並在 stderr 印一行 `WARN`。
