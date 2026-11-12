---
section: Fixed
topic: tools
issues: [3772]
created: 2026-11-12T15:49:17+00:00
---
- **da-tools migrate 的摘要與報告數字改為加總一致（tools；[#3772](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3772)）**：規則被字典導向黃金標準時，摘要先前印出「成功轉換 0 條規則 (✅ 2 完美, ⚠️ 1 已猜測)」——總數扣掉了黃金標準，括號內的分項沒扣；`migration-report.txt` 與 `--dry-run` 的計數也把同一條規則同時算在解析狀態與黃金標準裡，報告裡還會留下空的「✅ 完美解析的規則」區段。現在改用黃金標準的規則只算在黃金標準一桶：完美、已猜測、無法解析、黃金標準四桶互斥，加總等於規則總數；空的區段不再印出。產出的檔案內容不變。
