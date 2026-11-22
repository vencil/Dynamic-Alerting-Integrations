---
section: Changed
topic: confd-family
issues: [4097]
created: 2026-11-22T02:10:00+00:00
---
- **⚠️ breaking：`_metadata.db_type` 移除，租戶跑哪些 exporter 改用頂層的 `_expected_exporters` 宣告（exporter、tenant-api、portal、tools；[#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097)）**：v2.9 的 `db_type` 是 UI 分組用的字串，值沒有人檢查。新鍵是 exporter 種類的列表，例如 `_expected_exporters: [mariadb, redis]`；種類是有存在指標的 Rule Pack，`nginx`、`jvm` 也能宣告了。**遷移**：刪掉 `_metadata.db_type`，在租戶頂層（`_metadata` 旁）寫 `_expected_exporters`；沒有別名，還寫著舊欄位的設定在租戶檔、平台檔 `tenants:`、profile 都是 key verdict 的 error：舊值都是已知種類時，訊息印出要寫的那一行；否則說明有幾個值不是種類，並列出可用的種類。`da-tools validate-config`、`generate-routes --validate`（結束碼 1）會擋下；tenant-api 拒收新寫入舊欄位，原本就有的改其他鍵時只提醒；`da-guard key-verdict` 把它列為 error，結束碼仍是 0；只跑 `da-guard --config-dir` 的檢查不含這一項。⚠️ 宣告等於打開該種類的缺席告警（`TenantExporterAbsent`，critical），只宣告真的有被 scrape 的 exporter。值由 exporter 判：不是列表、不認得的種類、重複，同樣三層都是 error，而且一種都不送。tenant-api 清單／搜尋的 `db_type` 欄位與篩選參數改名 `expected_exporters`（字串陣列；舊參數回 400）；portal 已存的檢視若帶 `db_type` 篩選，套用時當成 `expected_exporters` 篩選，不必重存；`da-guard served-values` 的 schema 升為 v6。
