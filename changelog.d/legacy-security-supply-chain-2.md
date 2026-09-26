---
section: Fixed
topic: security-supply-chain
issues: [895, 902, 929, 942, 1058, 1243, 1337, 1932, 1933, 1970]
created: 2026-09-26T17:00:00+00:00
---
- **CVE 清理與夜掃告警路徑修復（helm、k8s、tenant-api、threshold-exporter、recipe-preview、ci）**：第三方映像逐張 triage，fixable HIGH/CRITICAL 由約 120 筆降到 62（其餘確認為上游未發版）：`configmap-reload` 換成 `prometheus-config-reloader`、`prom-label-proxy` 改釘 Docker Hub v0.14.0、grafana 12.4.6；並移除 Alertmanager manifest 上不存在的 `--web.enable-lifecycle`（該 manifest 原本無法啟動；BYO Alertmanager 文件也不再教客戶加它）。自建 Go 映像 builder 升 Go 1.26.5，recipe-preview 內建 promtool 升到 3.14.0、豁免清空。⚠️ `federation-gateway` 的 audit sidecar 改由 pinned commit 自行編譯 mtail，`auditLog.image.tag` 改為 `3.0.8-2`（chart 0.5.1）——升級前須先 build 並 push 該映像，否則 `ImagePullBackOff`。夜掃告警曾因 label 描述超過 API 上限連續多晚送不出、處置說明被 bash 截斷或誤執行，已修正並加守衛。詳 [#1243](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1243)、[#1058](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1058)、[#1337](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1337)。
