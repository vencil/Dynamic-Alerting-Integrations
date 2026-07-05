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

## Tenant-sanitized projection (ADR-021 Phase 1 (b) — [#609](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/609))

Off by default. When `tenantProjections` is set, the chart fans out a **second, sanitized** copy of each tenant's `federation_audit` rows into that tenant's native VictoriaLogs `(AccountID, ProjectID=0)` partition — so a tenant can query the platform's operational logs *about itself* without seeing infra topology or another tenant's rows. The platform full copy keeps flowing to the primary `victorialogs` sink (`0:0`) **unchanged**.

```yaml
# values.yaml — copy each tenant's id from the Git account registry
# (_account_registry.yaml). NEVER invent or reuse a retired AccountID.
tenantProjections:
  - tenantId: "tenant-alpha"   # must equal the audit JSON .tenant_id (JWT claim)
    accountId: 1000
```

| Property | How |
|---|---|
| **Fail-closed** | `tenant_project` (remap) runs `drop_on_abort` + `drop_on_error` + `reroute_dropped:false`. Non-audit / blank / unknown `tenant_id` / parse-error → `abort` → dropped from the tenant branch. Unmapped `account_id` → `tenant_route._unmatched` (no sink consumes it). No catch-all tenant sink — a mis-stamp cannot reach another tenant. |
| **Enrichment** | `tenant_id`→`AccountID` is an explicit committed map templated from `tenantProjections` (NOT a hash — collision = cross-tenant leak). |
| **Static N-sink** | One `vl_tenant_<id>` sink per tenant, each with a **constant** `AccountID` header. A single dynamic header-templated sink would mis-stamp mixed-tenant batches ([vectordotdev/vector#21402](https://github.com/vectordotdev/vector/issues/21402)). |
| **Sanitization (allowlist)** | The tenant event is **rebuilt from `tenantProjectionKeepFields` only** — fail-closed. A denylist would be fail-OPEN here: demux deep-merges the whole audit JSON, so the raw `.message` (which embeds `upstream`=`%UPSTREAM_HOST%`, the backend IP) and any unlisted/nested/future field would ride into the tenant partition. `_msg` is re-serialized from the safe fields; the raw line is discarded. Adding a field to the keep-list is the security-reviewed action. |
| **Uniqueness guard** | A render-time `{{ fail }}` rejects a duplicate `accountId` (would co-mingle two tenants into one partition) or duplicate `tenantId` (would mis-route to a foreign AccountID); `values.schema.json` rejects a non-integer/quoted `accountId`. `vector validate` would NOT catch these (serde_yaml dup-key = last-wins). |
| **Correlation** | `log_event_id` (time-sortable **UUIDv7**) **unconditionally** stamped in the shared `demux` stage (overwrites any producer-supplied value — the platform owns the join key) → identical in BOTH `0:0` and the tenant copy → on-call joins a redacted tenant row back to the full `0:0` row. |

Behavior is pinned by `vector test` (`tests/projection_tests.yaml`) + `tests/shared/test_vector_projection_vrl.py`. See [`platform-log-aggregation-runbook.md` §8](../../docs/internal/platform-log-aggregation-runbook.md) for the operator how-to and the `log_event_id` join SOP.

## RBAC

`kubernetes_logs` needs `list/watch/get` on `pods`, `namespaces`, `nodes` cluster-wide. The ClusterRole + binding ship with the chart; the pod also drops all capabilities except `DAC_READ_SEARCH` (the minimum to read root-owned hostPath log files).

## Install

> Since #1018 the canonical install namespace is the dedicated `vector` ns —
> a PSS `enforce=privileged` carve-out (`k8s/00-namespaces/namespace-vector.yaml`):
> the DaemonSet's 3 hostPath mounts are forbidden even by the PSS *baseline*
> profile, so this chart cannot live in a restricted-tier namespace. The chart
> itself stays ns-agnostic (`.Release.Namespace`). Migrating an existing
> `monitoring`-ns install: [`platform-log-aggregation-runbook.md` §1.1](../../docs/internal/platform-log-aggregation-runbook.md).

```bash
# Namespace first (PSS labels included)
kubectl apply -f k8s/00-namespaces/namespace-vector.yaml

# Default (federation-gateway only, sink at victorialogs.monitoring.svc:9428)
helm install vector ./helm/vector -n vector

# Override the sink namespace
helm install vector ./helm/vector -n vector \
  --set victorialogs.host=victorialogs.observability.svc

# Smoke-test (tail all pods on the node)
helm install vector ./helm/vector -n vector \
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
