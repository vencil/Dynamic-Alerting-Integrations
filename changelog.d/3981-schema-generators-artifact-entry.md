---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-22T10:40:00+00:00
---
- **四支產生已提交複本的工具改經共用的寫入入口（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：`gen_tenant_id_json`、`gen_am_duration_json`、`sync_schema`、`gen_recipe_contract` 的目標不是一般檔時（FIFO、目錄），原本會卡住不返回（`--check` 也會）或印 traceback；現在讀寫之前先判斷，結束碼 `2` 加一行點名該檔，所有目標都通過才寫第一個。寫入一般以原子取代完成；目標有 hard link、所在目錄不可寫、無法被 rename 覆蓋（mount point）、帶有取代檔保不住的延伸屬性，或屬於別人而保不住原 owner 時，印 `WARN` 後退回就地寫入，中斷可能留下不完整的檔（[#4106](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4106)）。改寫後的權限是 git checkout 會給的（`0644`，原本有 owner 執行位元則 `0755`）。四支都只需標準函式庫，不需要 PyYAML。
