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

## Compliance branch (Phase 3 — `additionalSinks`)

Vector's multi-sink design is what keeps the compliance branch reachable without reconfiguring the upstream pipeline. When strict compliance (tamper-evidence / WORM / legal hold) arrives, fan out the same demuxed stream to an external SIEM via the `additionalSinks` list — the source + transform are unchanged. See [#539 §4 Phase 3](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539).

```yaml
# values.yaml
additionalSinks:
  - name: splunk_compliance
    type: splunk_hec_logs
    inputs: [demux]                  # MUST be demux for VRL-tagged events
    endpoint: https://splunk.example.com:8088
    default_token: ${SPLUNK_TOKEN}   # provide via envFrom secret
    _buffer_when_full: drop_newest   # see below — never `block` for fan-out
    _buffer_max_events: 10000
```

### Back-pressure isolation (the #539 §2 hard rule)

Every `additionalSinks` entry is auto-wrapped with a `buffer:` block (unless the entry provides its own). The default is `when_full: drop_newest, max_events: 10000` — when the SIEM is slow or down, NEW events to that sink are dropped after the buffer fills, but the primary VictoriaLogs sink is **never** back-pressured.

This is the §2 hard rule made operational: SIEM downtime must not wedge primary delivery. The two `_buffer_*` knobs are stripped from the rendered Vector config and consumed by the chart template — they are NOT valid Vector sink keys.

**Do NOT set `_buffer_when_full: block` on a fan-out sink.** Block back-pressures upstream until the slow sink catches up — defeats the §2 rule. Only valid if the SIEM is the *system of record* (i.e. you are running this chart in compliance-only mode and `victorialogs` is disabled, in which case the SIEM IS the primary path).

### What "delegated to the SIEM" actually means

VictoriaLogs (like Loki) is **not** tamper-evident and has no WORM mode. Adding a SIEM fan-out moves three properties to the SIEM:

| Property | VictoriaLogs | SIEM |
|---|---|---|
| Tamper-evidence (hash chain / signed timestamps) | ❌ | ✅ (Splunk, Sumo, Elastic + immutable storage) |
| Legal hold (operator cannot delete during retention) | ❌ (operator with kubectl can `wget DELETE`) | ✅ (SIEM RBAC + retention policy) |
| Immutable retention window | ❌ (`-retentionPeriod` is operator-set) | ✅ (compliance regimes define) |

The platform's role ends at handing the SIEM the same demuxed stream; the SIEM owner runs the chain-of-custody story.

### Naming + collision rules

- Each `additionalSinks` entry MUST have a unique `name` — collision with the built-in `victorialogs` sink fails chart lint as a duplicate YAML key (fail-loud, intentional).
- Each entry's `inputs:` SHOULD be `[demux]` so the SIEM sees the same VRL-tagged stream (with `log_type` / `tenant_id` / etc.) as VictoriaLogs.
- Pointing `inputs:` directly at `kubernetes_logs` is supported but means the SIEM gets RAW Envoy log lines — usually not what compliance wants.
