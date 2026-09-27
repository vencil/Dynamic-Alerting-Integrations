---
section: Fixed
topic: cli-docs-accuracy
issues: [1818, 1196]
created: 2026-09-27T07:10:00+00:00
---
- **Domain Expert 快速入門整頁依真實格式重寫（docs；[#1818](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1818)、[#1196](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1196)）**：原頁的 Rule Pack 範例是 repo 裡不存在的 `data_mappings:`／`thresholds:`／`alert_rules:` 格式，改成從 `rule-pack-mariadb.yaml` 逐字節錄的三部分 Prometheus 規則，並補上閾值來源（租戶 conf.d、`_critical`、UTC 排程窗）與新增 key 要宣告、要跑的檢查；遷移、回測、lint 的命令改成工具真的接受的旗標，工具沒有的能力寫「尚未實作」並指向替代；三層治理表對齊 Custom Rule Governance。`migration-guide` §8 的 rate／比值兩列拿掉沒有任何 Rule Pack 在讀的 `mysql_slow_queries`／`mysql_innodb_buffer_pool`，改寫平台現況。
