---
section: Added
topic: rule-packs
issues: [4093]
created: 2026-11-21T06:30:00+00:00
---
- **⚠️ breaking：DB2 新增 `DB2StoragePathAlmostFull`，預設啟用（[#4093](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4093)）**：沒有設 `MAXSIZE` 的自動儲存 tablespace（預設建立的資料庫全部都是）在 `db2_tablespace_used_percent` 沒有資料，`DB2TablespaceAlmostFull` 對它們不會響；它們的實際上限是 storage path 所在的檔案系統。新告警看那個檔案系統的使用率（used / total），取租戶內最滿的路徑，超過 `db2_storage_path_used_percent`（預設 80）持續 5 分鐘發 warning。80 與 Zabbix Linux 範本的 warning 門檻相同；Db2 只對日誌檔案系統公布預設值（`db.log_fs_util`，warning 75），對 storage path 沒有。**誰會新收到告警**：storage path 與其他資料共用磁碟的部署（容器的共用磁碟、與 OS 同一個檔案系統）——數字反映整個檔案系統，實驗環境量到 85.5%。**升級動作**：重新部署 `rule-packs/exporters/db2.yaml`（多一支查詢與 `db2_storage_path_fs_total_bytes`、`db2_storage_path_fs_used_bytes`），並對監控帳號執行 `GRANT EXECUTE ON FUNCTION SYSPROC.ADMIN_GET_STORAGE_PATHS TO USER MONITOR`。只升 rule pack、沒重新部署 exporter 設定時這條告警沒有資料、不會響也不會報錯；有部署但缺授權時查詢失敗，由 `DB2QueryStalled` 指出。pureScale／DPF 上未實測。不想收到的租戶把該 key 設為 `"disable"`。本版內建表摘要 `3f4ed6d06f1b`。
