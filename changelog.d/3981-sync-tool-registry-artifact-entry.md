---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-25T02:31:00+00:00
---
- **`sync_tool_registry.py`（`make sync-tools`）改經共用的寫入入口（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：要改的頁（jsx loader、hub 頁）都算完、都判斷過才開始寫，寫入改經 `write_repo_artifact`：原子取代（hard link 改為就地寫入，stderr 多一行 WARN）；權限照 checkout（`0644`，可執行檔 `0755`；原本維持原權限，例如 `0444`）。以 `>>` 把 stdout 附加到一頁要改的頁時，原本是結束碼 `0`，照寫、報告接在後面，現在是 `2` 加一行點名該頁，兩頁都不寫（寫入前已印出的行，例如 `Loaded … tools`，仍會進檔）；附加到沒有要改的頁時行為不變。寫入失敗原本是 `unexpected internal error`，現在是 `2` 加一行。讀到 FIFO 原本卡住，現在是 `2`；讀取錯誤與非 UTF-8 原本是 `unexpected internal error`，現在是 `2` 加一行（`--dry-run` 亦同），兩頁都不寫。`[flow_map] Updated …`／`[hub] Applied …` 訊息（文字不變）改在該頁寫完之後印出。
