---
section: Fixed
topic: federation
issues: [1295]
created: 2026-11-12T23:59:30+00:00
---
- **⚠️ Breaking：federation-gateway 在 default-deny 的 namespace 裡不再完全連不出去（federation-gateway chart 0.8.0；[#1295](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1295)）**：chart 的 NetworkPolicy 只有 ingress，而 `k8s/03-monitoring` 對整個 namespace 預設拒絕 egress，gateway 連 DNS 與 upstream 都到不了。現在 policy 也限制 egress：只放行 DNS 與 upstream 所在 namespace 的 TCP `upstream.port`。namespace 取 `networkPolicy.upstreamNamespace`，未設則從 `upstream.host` 讀（`<svc>` 為本 release 的 namespace，`<svc>.<ns>.svc` 為 `<ns>`）；其他寫法（外部網域、IP）會拒絕 render，要設 `upstreamNamespace`，或設 `networkPolicy.restrictEgress: false` 並自行放行。規則比對的是 upstream pod 的埠，Service port 與 targetPort 不同時設 `networkPolicy.upstreamPort`。
