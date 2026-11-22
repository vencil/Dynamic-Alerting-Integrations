---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-22T10:40:00+00:00
---
- **四支產生已提交複本的工具改經共用的寫入入口（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：`gen_tenant_id_json`、`gen_am_duration_json`、`sync_schema --update`、`gen_recipe_contract --update` 的輸出目標不是一般檔時（FIFO、目錄），原本是卡住不返回或印 traceback；現在是結束碼 `2` 加一行點名該檔，寫入是原子的。`gen_tenant_id_json` 會先檢查全部複本才寫第一個，其中一個被拒絕時其他的不動。`--check` 模式不變。
