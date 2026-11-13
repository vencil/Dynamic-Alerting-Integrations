---
section: Fixed
topic: federation
issues: [3807]
created: 2026-11-13T07:00:00+00:00
---
- **⚠️ Breaking：federation-proxy 在 default-deny 的 namespace 裡不再完全連不出去（federation-proxy chart 0.2.0；[#3807](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3807)）**：chart 的 NetworkPolicy 只有 ingress，而 `k8s/03-monitoring` 對整個 namespace 預設拒絕 egress，prom-label-proxy 連 DNS 與 Prometheus 都到不了，federation 的每個查詢都在 proxy 這一跳失敗。現在 policy 也限制 egress：只放行 DNS 與 upstream 所在 namespace 的 upstream port。namespace 與 port 從 `upstream.url` 讀（`<svc>` 為本 release 的 namespace，`<svc>.<ns>.svc` 為 `<ns>`；port 取 URL 的，沒寫就依 scheme），也可用 `networkPolicy.upstreamNamespace` 指定；其他寫法（外部網域、IP）會拒絕 render，要設 `upstreamNamespace`，或設 `networkPolicy.restrictEgress: false` 並自行放行。
