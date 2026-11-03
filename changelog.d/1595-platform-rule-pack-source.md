---
section: Changed
topic: platform-self-monitoring
issues: [1595]
created: 2026-11-01T10:51:32+00:00
---
- **平台自監控告警改由 rule pack 來源生成（rule-packs；[#1595](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1595) D1a）**：平台告警（含 `Watchdog`）的來源移到 `rule-packs/rule-pack-platform.yaml`，`k8s/03-monitoring/configmap-rules-platform.yaml` 改由 `make rulepack-configmaps` 生成、受 `check_rulepack_sync.py` 守一致；ConfigMap 名稱、資料鍵與 Prometheus 掛載不變。⚠️ 要改平台告警請改 pack 來源——直接改 ConfigMap 會被 drift 閘門擋下。哪些 pack 是平台 pack 只在 `_lib_consumer_read.PLATFORM_PACKS` 宣告一次，alert_source RESERVED／Watchdog 唯一性等規則契約、平台資料目錄與 `operator-generate` 都讀它。`operator-generate` 如何輸出平台 pack 見 D1b 的條目。`da-tools lint --rule-pack-dir` 依同一宣告略過平台 pack（租戶 policy 不適用）。
