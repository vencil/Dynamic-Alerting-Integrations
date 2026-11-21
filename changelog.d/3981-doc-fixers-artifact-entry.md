---
section: Fixed
topic: da-tools
issues: [3981]
created: 2026-11-21T20:00:00+00:00
---
- **五支會就地改寫追蹤檔的工具改經生成物入口寫檔（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：`generate_rule_pack_readme.py --update`、`generate_alert_reference.py`、`add_frontmatter.py`、`check_repo_name.py --full-scan --fix`、`check_doc_links.py --fix-anchors` 以前直接 `open(w)` 或 `write_text`：寫到一半失敗會留下被截斷的檔，並以 traceback 結束（結束碼 `1`），或各自印一行自己的訊息。現在都經 `write_repo_artifact` 原子取代，寫入失敗是結束碼 `2` 加一行點名該檔，舊內容保留。目標不是一般檔（例如 FIFO）時，兩支產生器以前會卡住不返回，現在在開檔前拒絕。`generate_alert_reference.py` 先檢查中、英兩個目標再寫，其中一個被拒絕時不會只重生另一個。⚠️ 改寫後的檔案權限是 git checkout 會給的（`0644`，原本有 owner 執行位元則 `0755`），不再沿用舊檔的其他位元。
