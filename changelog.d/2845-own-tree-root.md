---
section: Fixed
topic: da-tools
issues: [2845]
created: 2026-10-10T23:40:00+08:00
---
- **出貨工具找 repo 內的 schema／rule pack／conf.d／版號時，只認自己所在的那棵樹（[#2845](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2845)）**：`validate-config`、`generate-routes`、`describe-tenant` 與讀告警索引的 `config-diff`／`patch-config` 等，原本以「第一個帶 `.git`／`Makefile`／`pyproject.toml` 的祖先目錄」當根目錄。本樹根目錄沒有這些檔時（例如只把 `scripts/` 複製或 vendor 進另一個 repo），會改用外層專案的 schema、rule pack、conf.d 與版號：外層若帶著一份不同版本的 `tenant-config.schema.json`，`validate-config` 與 `generate-routes --validate` 會讓不合法的 webhook URL 以 rc 0 通過，`describe-tenant` 則會描述外層的租戶。現在根目錄一律取「包住這支工具的 `scripts/tools` 所在目錄」，找不到本樹的資料時照既有方式報錯，不再採用外層的檔案。checkout、git worktree、`git archive` 與 da-tools 映像的行為不變。
