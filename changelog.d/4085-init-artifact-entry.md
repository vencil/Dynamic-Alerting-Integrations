---
section: Changed
topic: da-tools
issues: [4085]
created: 2026-11-20T00:40:00+00:00
---
- **⚠️ `init` 寫出的檔改為整份取代、權限照 checkout（[#4085](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4085)）**：`da-tools init -o` 底下由 Python 寫的檔（CI、kustomize、Helm values、`.pre-commit-config.da.yaml`、`.da-init.yaml`；conf.d 照舊由 `da-guard write` 寫）改經生成物入口原子取代。權限由 `0600` 改為 `0644`（不讀 umask；既有檔的執行位保留），與 conf.d 檔及 git checkout 一致。`--force` 重跑寫到一半失敗時每個檔都保留上一次的內容（以前第一個寫到的檔被清空）。既有路徑是裝置（含指向 `/dev/null` 的 symlink）或本行程 stdout 重導的檔時結束碼 `0` → `2`；FIFO 照舊是 `2`，訊息改為 `not a regular file`。有多個 hard link、屬於別人而可寫、或目錄不可寫的既有檔改為就地寫入並印一行 `WARN`。
