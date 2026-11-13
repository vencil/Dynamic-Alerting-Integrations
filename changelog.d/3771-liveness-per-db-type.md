---
section: Changed
topic: rule-packs
issues: [3771]
created: 2026-11-13T10:30:00+00:00
---
- **⚠️ breaking：exporter 存活改以「租戶 × db_type」判斷，`_metadata.db_type` 可寫成列表（rule-packs、exporter、tenant-api；[#3771](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3771)）**：`db_type` 可寫成字串或不重複的列表（如 `[mariadb, redis]`），字串等同一元素列表；租戶檔的值整份取代平台檔的，不聯集。exporter 對每個宣告的種類各吐一條 `tenant_expected_exporter`；liveness pack 新增 `tenant:exporter_present{tenant,db_type}`，依各 exporter 自己的指標判斷「這一種」有沒有在回應。`TenantExporterAbsent` 改為逐 db_type 判斷：同租戶 redis 健康、mariadb 唯一副本掛時照樣 critical。`MassExporterOutage` 的門檻與 summary 改以組計。**遷移**：tenant-api 清單／搜尋的 `db_type` 由字串改為字串陣列，讀它的整合要改；`db_type` 篩選比對任一種類；policy DSL 對 list 欄位逐元素比（`not_equals` 要每個元素都不等），既有單值規則讀法不變。Prometheus 須載入新的 `liveness-recording.yml`（自建部署請一併掛載）。
