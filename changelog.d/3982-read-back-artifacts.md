---
section: Changed
topic: da-tools
issues: [3982]
created: 2026-11-18T22:00:00+00:00
---
- **工具會讀回的輸出改為整份取代（[#3982](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3982)）**：`config-history snapshot` 的 `.da-history/history.json` 與 `snap-N/` 副本改以原子取代寫入、權限 `0600`：寫到一半失敗時舊索引完整保留（以前截斷，之後連 `config-history log` 都以 `unexpected internal error (JSONDecodeError)` 結束碼 `2`）；`history.json` 以前照 umask（多為 `0644`），硬連結的 `history.json` 改為換成新 inode（另一個名字保留舊內容），symlink 照舊寫進目標。`compile_custom_alerts.py --out` 同樣整份取代，失敗時留舊 pack（以前截斷，`--check` 把半份當合法舊 pack 只報 drift）；`--out -` 與空值在解析時結束碼 `2`，FIFO、`/dev/null` 等裝置、目錄在讀回與寫入前結束碼 `2`（以前 FIFO 會卡住，`/dev/null` 結束碼 `0`）。
