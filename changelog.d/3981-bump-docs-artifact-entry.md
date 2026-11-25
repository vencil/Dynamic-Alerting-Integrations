---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-25T08:00:03+00:00
---
- **`bump_docs.py`（版號 bump、`--sync-counts`／`make sync-counts`、`--init-changelog`、`make version-check`）改經共用的寫入入口（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：每個目標只讀一次（同一檔的多條規則、或經 symlink 的另一個名字，都改同一份內容），全部算完、判斷過才開始寫，只寫內容有變的檔，寫入改經 `write_repo_artifact`：原子取代（hard link 改為就地寫入，stderr 多一行 WARN）；權限照 checkout（原本一律改成 `0644`，現在可執行檔保留 `0755`）。以 `>>` 把 stdout 附加到要改的檔時，原本是結束碼 `0`、照寫、報告接在後面，現在是 `2`，不寫任何檔（版號 bump 開始時印的標頭仍會進檔）。目標或版號來源是 FIFO 時（各模式，含 `--check`／`--what-if`／`--show-current`）原本卡住；是目錄、讀不到、非 UTF-8 時原本是 `unexpected internal error`，寫入模式下前面的檔常已改寫。現在都是 `2` 加一行，不寫任何檔。寫入被拒（例如不可寫）也是 `2` 加一行，但在它之前的檔可能已寫出（#4250）。
