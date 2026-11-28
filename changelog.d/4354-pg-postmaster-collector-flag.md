---
section: Changed
topic: rule-packs
issues: [4354, 3833]
created: 2026-11-28T10:54:00+00:00
---
- **PostgreSQL pack 標明要用的 postgres_exporter 版本與旗標（rule-packs；[#4354](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4354)）**：`PostgreSQLRecentRestart` 讀的 `pg_postmaster_start_time_seconds` 來自 postmaster collector，postgres_exporter 預設不開；沒帶 `--collector.postmaster` 的 exporter 上這條告警沒有資料。規則沒有改，改的是把這個前提寫出來：`rule-packs/ALERT-REFERENCE.md` 的 PostgreSQL 段首列出要帶的旗標，`rule-packs/rule-pack-postgresql.yaml` 檔頭的生成區塊列出 pack 核對過的 exporter 版本（v0.21.1）、映像 digest、啟動旗標與核對對象（PostgreSQL 16.15 的 primary 與 standby）。請在租戶的 postgres_exporter 加上 `--collector.postmaster`。其他版本或旗標組合沒有核對過。
