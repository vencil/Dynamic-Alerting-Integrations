---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-22T10:40:00+00:00
---
- **兩支產生已提交複本的工具改經共用的寫入入口（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：`gen_tenant_id_json`、`gen_am_duration_json` 的輸出目標不是一般檔時（FIFO、目錄），原本是卡住不返回或印 traceback；現在是結束碼 `2` 加一行點名該檔。寫入一般以原子取代完成；目標有 hard link、屬於別人、帶延伸屬性或目錄不可寫時，印 `WARN` 後退回就地寫入，中斷可能留下不完整的檔（[#4106](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4106)）。`gen_tenant_id_json` 會先檢查兩個複本都是一般檔才寫第一個。寫入模式現在需要 PyYAML（`make tenant-id-json`、`make am-duration-json`）；`--check` 模式不變，仍只需標準函式庫。
