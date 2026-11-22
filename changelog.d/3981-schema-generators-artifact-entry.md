---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-22T10:40:00+00:00
---
- **四支產生已提交複本的工具改經共用的寫入入口（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：`gen_tenant_id_json`、`gen_am_duration_json`、`sync_schema`、`gen_recipe_contract` 的目標是 FIFO 時原本會卡住不返回（`--check` 也會），目錄等非一般檔依工具不同是 traceback 或各自的一行；現在讀寫之前先判斷，一律結束碼 `2` 加一行點名該檔，所有目標都通過才寫第一個。要改寫的目標若剛好是本行程的 stdin／stdout／stderr 也會被拒絕（只讀的不會，例如 `--check < 目標`）。寫入一般以原子取代完成；做不到時（例如目標有 hard link、所在目錄不可寫、無法被 rename 覆蓋，或取代檔保不住延伸屬性或原 owner；完整條件見 `_lib_atomic_write.py` 檔頭）印 `WARN` 後退回就地寫入，中斷可能留下不完整的檔（[#4106](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4106)）。改寫後的權限是 git checkout 會給的（`0644`，原本有 owner 執行位元則 `0755`）；就地寫入別人的檔時保留原權限並印 `WARN`。四支都只需標準函式庫，不需要 PyYAML。
