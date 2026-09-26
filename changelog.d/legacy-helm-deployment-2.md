---
section: Fixed
topic: helm-deployment
issues: [682, 1176, 1246, 1302, 1320, 1321]
created: 2026-09-26T17:00:00+00:00
---
- **部署起得來、跑的就是宣告的版本（helm、k8s）**：`mariadb-instance` 改 pin 實際存在的 `mariadb:11.8.8`（原 `11.8.1` 不存在，全新環境 `ImagePullBackOff`）；da-portal Tier 1 static-only profile 不再因渲染出無效 nginx 設定而 crashloop；Tier 2 profile 移除陳舊的 oauth2-proxy tag 覆寫、改繼承 `values.yaml`，manifest 與 SBOM 不再指名錯的版本（pull 行為不變，[#1302](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1302)），overlay 同時納入自動升版與 CVE 掃描。Prometheus 補上真正的 `config-reloader` sidecar，Rule Pack ConfigMap 變更免重啟即熱更新，並新增 reload 失敗與 sidecar 異常的平台告警；⚠️ 以 `subPath` 掛載的 `prometheus.yml` 仍須重啟 Pod；套用本變更會觸發 rolling update，`replicas: 1`＋`emptyDir` 的 Prometheus TSDB 歷史歸零（[#1246](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1246)）。出貨的 `_defaults.yaml` ConfigMap 以註解說明 `optional_overrides` 各 key 不填即靜默，型別錯誤時 render 以具名訊息失敗（[#1321](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1321)）。
