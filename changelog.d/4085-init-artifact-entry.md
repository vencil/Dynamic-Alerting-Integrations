---
section: Changed
topic: da-tools
issues: [4085]
created: 2026-11-20T00:40:00+00:00
---
- **⚠️ `init` 寫出的檔改為整份取代、權限照 checkout（[#4085](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4085)）**：`da-tools init -o` 底下由 Python 寫的檔（CI、kustomize、Helm values、`.pre-commit-config.da.yaml`、`.da-init.yaml`；conf.d 照舊由 `da-guard write` 寫）改經生成物入口原子取代。權限由 `0600` 改為 `0644`（不讀 umask；既有檔的執行位保留），與 conf.d 檔及 git checkout 一致。取代是逐檔的：`--force` 重跑寫到一半失敗時，被原子取代的檔不是舊的完整內容就是新的完整內容，不會是空檔或半份（以前失敗的那個檔被清空）；不是整次重跑的回復。既有路徑是裝置（含指向 `/dev/null` 的 symlink）或本行程的標準串流（stdin／stdout／stderr）時結束碼 `0` → `2`；這些與 FIFO、目錄都在寫任何檔之前拒絕，樹維持原樣。有多個 hard link、屬於別人而可寫（權限不動）、帶延伸屬性、或目錄不可寫的既有檔改為就地寫入並印 `WARN`（中途失敗時留前半段）；執行者不屬於舊檔的群組時，取代後的檔群組改為執行者的。
