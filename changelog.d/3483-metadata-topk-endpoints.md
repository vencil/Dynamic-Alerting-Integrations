---
section: Changed
topic: rule-packs
issues: [3483]
created: 2026-11-01T14:45:33+00:00
---
- **平台 Prometheus 改為逐 pod 抓取平台元件，副本掛掉看得到（k8s；breaking，[#3483](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3483)）**：`platform-components` job 從 `role: service` 改成 `role: endpoints`，每個 pod 抓一次（只留標註 port 上的那個 target），並加上 `pod` label。過去每次 scrape 只打到 Service 背後其中一個副本，`up{job="threshold-exporter"}` 看不到掛掉的副本、`ThresholdExporterTooFewReplicas` 恆觸發。現在每個 `user_*`／`tenant_*`／`da_*` 序列各有一份 per 副本：附帶的 Grafana 面板改用 `max without(instance, pod)` 計數，kubernetes pack 的 crashloop／imagepull opt-in join 右側改為 `max by(tenant)`。自己寫的面板或規則若直接計數、或拿這些指標當 join 的右側，要照同樣方式收斂；平台 Service 的 `prometheus.io/port` 必須是 pod 實際監聽的 port。
