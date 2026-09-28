---
section: Fixed
topic: da-tools
issues: [1196]
created: 2026-09-27T21:23:20+00:00
---
- **baseline 建議的閾值寫進 rule pack 真的在讀的 key，單位也對了（da-tools；[#1196](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1196)）**：`da-tools baseline` 先前把每個觀測指標拼成 `mysql_<名稱>` 印成 patch-config 建議。`mysql_memory`、`mysql_disk_io`、`mysql_slow_queries` 沒有任何告警會讀；`mysql_cpu` 會落到 threads_running 的閾值，等於把容器 CPU% 寫成執行緒數。現在 cpu 與 memory 改算「佔 limit 的 %」，算法與 rule pack 的 `container_cpu`／`container_memory` 告警相同，也不再只看名為 mariadb 的容器；因為有上界，不再乘係數給門檻，改為對照平台預設，已頂到預設時建議先調高 limit。slow_queries 與 disk_io 沒有租戶 key，改印原因。portal platform-demo 的錄製輸出已重錄。
