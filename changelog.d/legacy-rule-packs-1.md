---
section: Added
topic: rule-packs
issues: [944, 1200]
created: 2026-09-26T17:00:00+00:00
---
- **新增資料庫與 Kubernetes 告警覆蓋（rule packs）**：MariaDB 補上 async replication（IO／SQL thread 斷線為 critical、`MariaDBReplicationLag` warning 30s 預設啟用）與 semi-sync 降級／replica 歸零告警；DB2 新增 `DB2HighLockWaitTime`（`db2_lock_wait_time` 平台預設 10）。kubernetes pack 新增 `ContainerOOMKilled`／`Critical`（預設啟用）、CFS 掐頸 `PodContainerCPUThrottled`（新 key `container_cpu_throttle`，warning 25）、HA 副本降級 `TenantHAReplicasDegraded`、依租戶歸因的 `NodeNotReady`，以及磁碟 recipe 收不到資料時的 `CustomRecipeDiskInert` 與租戶磁碟 IOPS／吞吐 recipe（[#944](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/944)、[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）。⚠️ OOM、CFS throttle 與磁碟 I/O 需要 reference Prometheus 的 cAdvisor keep-list 一併更新才收得到資料，請與 rule pack 同批部署。
