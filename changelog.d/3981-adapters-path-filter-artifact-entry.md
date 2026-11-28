---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-25T13:04:06+00:00
---
- **`gen_agent_adapters` 與 `workflow_path_filter` 的寫入改經 `write_repo_artifact`（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：兩支都改為所有目標判斷過才開始寫，並逐檔列出寫出的檔；原子取代，hard link、別人的檔、目錄不可寫時改為就地寫入，並在 stderr 多一行 WARN。寫入（`gen_agent_adapters` 含刪除過期 adapter）被拒或失敗時，原本印 traceback 加 `unexpected internal error`（`gen_agent_adapters` 為 `1`，與漂移同碼）；現在都是結束碼 `2` 加一行。在它之前的檔可能已寫出或刪除（#4250），已寫出、已刪除的檔都在當下逐一列出。遇到 FIFO 原本卡住：adapter 是 FIFO 時，`gen_agent_adapters --generate` 結束碼 `2`，不寫任何檔；workflow 或 trace 檔是 FIFO 時，`workflow_path_filter` 結束碼 `2`。`gen_agent_adapters` 只寫內容或權限不對的 adapter（逐檔印 `wrote <path>`），權限仍固定 `0644`；摘要行改為 `✅ N adapter file(s) in step with the .agents/ SSOT`（有刪除時接 `; K stale one(s) removed`），`removed stale adapter: <path>` 改在刪除時印出。它原本載入 `paths_map` hook 來驗證 map，連帶把整個行程的錯誤結束碼改成 hook 協定的 `1`；驗證器抽成 `scripts/session-guards/_paths_map_schema.py`，hook 與產生器共用，產生器不再載入 hook。`workflow_path_filter` 改寫的 workflow 與 trace 檔改照 checkout 的權限（`0644`，可執行檔 `0755`），原本保留檔案既有的權限。
