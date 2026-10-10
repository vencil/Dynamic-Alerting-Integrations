---
section: Fixed
topic: confd-reader-consistency
issues: [1822]
created: 2026-10-10T02:33:30+00:00
---
- **`deprecate_rule` 寫入前先問 exporter，不再對 legacy 拼法與被整份丟掉的載體說「完成」（tools、exporter；[#1822](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1822)）**：預覽與 `--execute` 都先跑新的 `da-guard key-refs`，由 exporter 自己的程式碼回答兩件事。一、租戶檔用 alias 表的舊名（例如 `mysql_cpu`）、`_critical` 或任何維度鍵拼法寫的同一個 metric，現在與 canonical key 一樣列為引用並移除（子目錄 `_` 檔裡的則列為殘留）；先前只刪 canonical key、印「下架完成」，留下的舊名在下架後被 exporter 的 key 驗證報為 `unknown key ... not in defaults`。二、root 層 `_` 前綴檔在本輪寫入後 exporter 仍讀不進去時（例如 `optional_overrides: 5`），預覽與執行都 rc 1、不寫入；先前預覽 rc 0。找不到 da-guard（`$DA_GUARD_BINARY` → `$PATH`）或它答不出來時印「未體檢」與原因，行為與 rc 照舊。新增的 `da-guard key-refs` 不經 `da-tools guard` 轉發。
