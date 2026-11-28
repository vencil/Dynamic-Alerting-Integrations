---
section: Changed
topic: rule-packs
issues: [4178]
created: 2026-11-28T01:34:43+00:00
---
- **基礎設施指標只以 `namespace` 標示租戶，三種安裝方式一致（rule-packs、tools；[#4178](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4178)）**：平台附的 Prometheus 設定不再在 `kubelet-cadvisor`／`kubelet-volume-stats` 兩個 job 把 `namespace` 寫成 `tenant`（`kubelet-cadvisor` 的節點 label labelmap 也移除）；kubernetes pack 的 `tenant:kubelet_volume_stats:count` 改從 `namespace` 推出租戶，Prometheus Operator 安裝上 `CustomRecipeDiskInert` 不再誤報；自訂告警編譯器讀 `container_*`／`kubelet_*`／`kube_*` 時，序列已帶 `tenant` 就沿用、沒有才從 `namespace` 補上（前綴由 threshold registry 生成），磁碟與 IOPS recipe 在 Operator 安裝上也會觸發。`byo-check` 新增一步，指出仍在寫 `tenant` 的抓取設定。Federation 整合指南的中央 `match[]` 與邊緣 remote_write 另送租戶 namespace 的 `container_*`／`kubelet_*`，集中評估時這類 recipe 照常觸發（`kube_*` 不送）。⚠️ Breaking：租戶自訂規則對 cAdvisor／kubelet 指標寫 `tenant="<租戶>"` 改為 ERROR，請改寫成 `namespace="<租戶>", tenant=""`；照舊文件加過 `namespace→tenant` relabel 的自建 Prometheus 請移除它，否則上述新寫法選不到序列；federation 部署請照指南補上第二條 selector。
