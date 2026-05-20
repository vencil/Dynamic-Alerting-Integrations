# vector Helm chart

Node-level log shipper for the platform log-aggregation pipeline ([#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539) Phase 1).

Built on **Vector** (`timberio/vector:0.55.0-distroless-libc`) — sink-agnostic, VRL-transformable. The Vector pick over Fluent Bit / Promtail / Alloy is recorded in the source issue §3 (Promtail deprecated; Alloy loses home turf with VictoriaLogs; VRL is the right tool for the demux that matters).

## Pipeline

```
kubernetes_logs (source, this-node pods only — VECTOR_SELF_NODE_NAME)
        │
        ▼
demux (transform, VRL)
   │  parse_json(.message)
   │    success → log_type=federation_audit + merge parsed fields
   │    fail    → log_type=gateway_operational (raw .message kept)
   │  stream-field promotion: app / k8s_namespace
        │
        ▼
victorialogs (sink — elasticsearch _bulk API → /insert/elasticsearch/)
```

## Critical VRL decisions (from #539 §3)

| Decision | Why |
|---|---|
| **Demux on parse-success**, not `exists(.tenant_id)` | JWT-failure requests have no `tenant_id` (jwt_authn rejects before claim injection) but are forensically valuable (attack-scan evidence) |
| **Operational logs not dropped** — routed to `log_type=gateway_operational` | Without this the pipeline loses Envoy operational visibility — strictly worse than today. With this, those logs become centrally queryable for the first time. |
| `pod_name`, `token_id`, `query` are **data fields**, not stream fields | HPA churn / high cardinality would explode the stream index; bounded keys only on `_stream_fields` |

The stream-field set is locked at the values level (`streamFields`) — changing it after data is ingested splits the stream tree and requires a re-index. The default `[app, k8s_namespace, log_type, tenant_id, status]` matches #539 §3's load-bearing schema table.

## Phase 1 scope

`source.extraLabelSelector` defaults to `app.kubernetes.io/name=federation-gateway`. Per #539 §7 non-goals this is **not** a general platform log roll-up — every new consumer opens its own ticket. When #552 (chargeback) lands, extend the selector or add a second source.

## RBAC

`kubernetes_logs` needs `list/watch/get` on `pods`, `namespaces`, `nodes` cluster-wide. The ClusterRole + binding ship with the chart; the pod also drops all capabilities except `DAC_READ_SEARCH` (the minimum to read root-owned hostPath log files).

## Install

```bash
# Default (federation-gateway only, sink at victorialogs.monitoring.svc:9428)
helm install vector ./helm/vector -n monitoring

# Override the sink namespace
helm install vector ./helm/vector -n monitoring \
  --set victorialogs.host=victorialogs.observability.svc

# Smoke-test (tail all pods on the node)
helm install vector ./helm/vector -n monitoring \
  --set source.extraLabelSelector=""
```

## Compliance branch (Phase 3)

Vector's multi-sink fan-out is the design seam that keeps the compliance branch reachable. When a tamper-evident store is required, add a second sink (e.g. an external SIEM) alongside `victorialogs` — the source + transform are unchanged. See [#539 §4 Phase 3](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539).
