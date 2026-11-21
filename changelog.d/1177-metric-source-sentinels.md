---
section: Added
topic: rule-packs
issues: [1177]
created: 2026-11-21T10:30:00+00:00
---
- **Kubernetes pack 新增兩條來源哨兵 `ContainerMetricsMissing`、`PodStateMetricsMissing`（[#1177](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1177)）**：pack 裡的告警都要序列「在場」才會響，kubelet／cAdvisor 或 kube-state-metrics 停止被抓取時序列變 stale，告警跟著安靜，看起來和一切正常沒有分別。兩個來源現在互相對照：kube-state-metrics 說某個容器在運行（現在與 5 分鐘前都是）、cAdvisor 卻 5 分鐘沒有它的樣本，再持續 1 分鐘發 `ContainerMetricsMissing`（CPU、記憶體、節流、OOM 告警對它是盲的）；反過來 cAdvisor 有容器、kube-state-metrics 沒有該租戶任何 Pod 持續 5 分鐘，發 `PodStateMetricsMissing`（重啟迴圈、映像拉取、節點、副本類告警是盲的）。都是 warning、每個租戶（多叢集時每個叢集）一條、維護模式靜音，不需要設定。CrashLoopBackOff 中的容器與剛啟動的容器不算。**誰會新收到告警**：載入了 kubernetes pack、卻沒有抓 cAdvisor 或 kube-state-metrics 的部署（那些告警本來就從未生效），以及兩個抓取 job 只有一邊寫 `da_cluster` 的部署。節點 NotReady 時會和 `NodeNotReady` 一起響。**看不到的**：兩個來源同時消失（要靠抓取 job 自己的 `up`）；kube-state-metrics 還在回應但內容凍結或被過濾掉一部分。
