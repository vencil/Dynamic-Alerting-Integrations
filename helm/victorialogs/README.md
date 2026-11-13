# victorialogs Helm chart

Central log store for the platform log-aggregation pipeline ([#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539) Phase 1).

Built on **VictoriaLogs** (`victoriametrics/victoria-logs:v1.53.0`) — single binary, no object-storage ring / compactor / memcached. The operational-simplicity choice over Loki / ELK is recorded in the source issue §3.

Consumer #1: federation-gateway audit log (ADR-020 IV-2f, [#511](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/511)).
Consumer #2: chargeback query log ([#552](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/552)).

## Architecture (Phase 1)

```
federation-gateway (stdout JSON + stderr operational)
    │
    ▼
node DaemonSet (Vector)   ── helm/vector chart
    │
    ▼
VictoriaLogs (single pod) ── this chart
    │
    ▼
Grafana datasource / LogsQL
```

**Hard rule (#539 §2):** producers MUST NEVER HTTP-push directly here. Producer → stdout, delivery → shipper. Log-store downtime must not wedge the federation gateway. This chart deliberately exposes no direct-push convenience knob.

## Topology

Single-pod by design — VictoriaLogs is a single-binary store sized for this platform's volume (tens of pods, JSON-line audit). A clustered topology would re-introduce the Loki-style operational tax this selection rejected; revisit only if (a) ingest rate outgrows a single pod or (b) compliance requires hot-standby of the store itself.

`strategy: Recreate` because the PVC is RWO — a `RollingUpdate` would deadlock on volume re-attach.

## Retention

`retentionPeriod: 30d` is the conservative entry point per [#539 §5](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539): "typical ops/forensic 30–90 days. Revisit when an explicit compliance requirement names a window." Override per environment via Helm values.

## Endpoints

After install, the in-cluster URLs are:

| Use | URL |
|---|---|
| Vector elasticsearch sink | `http://victorialogs.<ns>.svc:9428/insert/elasticsearch/` |
| Grafana datasource | `http://victorialogs.<ns>.svc:9428` |
| LogsQL HTTP query | `http://victorialogs.<ns>.svc:9428/select/logsql/query` |
| `/metrics` (Prometheus scrape) | same port; annotations on the Service |

The NetworkPolicy is rendered from `auth.consumers` itself: each consumer's
`from` (its `namespace`, its `podLabels`, or both) is one row, so who may
connect and what it may do are one list. `podLabels` alone match pods in this
namespace only; a consumer installed elsewhere needs `namespace` (Vector has
both). Prometheus is not a consumer — it scrapes `/metrics` and is admitted by
`networkPolicy.scrapers` (default `app: prometheus`). Since chart 0.6.0,
`networkPolicy.allowedPodSelectors` / `allowedNamespaces` fail the render; with
`helm upgrade --reuse-values` use `--reset-then-reuse-values`. The connections
the documented install depends on are evaluated end to end, one per consumer,
in `tests/k8s/test_log_store_connectivity.py`.

### Consumer authorization (#1295)

A vmauth sidecar owns the Service port; VictoriaLogs listens on loopback only.
`auth.consumers` lists who may connect and on which paths (`vector`: `/insert`
only; the readers: `/select` only), with each password in an operator-created
Secret (hex, no trailing newline: `--from-literal=password="$(openssl rand -hex 32)"`).

| `auth.mode` | Requests without credentials |
|---|---|
| `enforce` (default) | only `/health`, `/ping`, `/metrics` |
| `observe` | reach every path — the migration step, **not a boundary** |

In `enforce`, every endpoint above needs the consumer's Basic Auth
credentials, and the chart refuses to render until every consumer in
`auth.consumers` names its Secret (the error lists the ones missing). Remove
consumers you do not run. Each consumer chart sends its credential from
`victorialogs.auth.existingSecret`; Grafana from the Secret
`victorialogs-grafana`. Upgrading from a chart whose default was `observe`:
set `auth.mode: observe` explicitly, wire every consumer, check they
authenticate, then drop it — `docs/internal/platform-log-aggregation-runbook.md` §1.2.

On `helm upgrade` the chart also looks up every Secret `auth.consumers` names
and refuses if one is missing or has no non-empty `password` — the strategy is
Recreate, so the running pod would go first and the new one would never start.
`helm template` and first installs skip this. The lookup needs `get secrets` in
the namespace; a deployer without it sets `auth.checkSecretsOnUpgrade: false`.

## Install

```bash
# One Secret per consumer you run (hex, no trailing newline); the consumer
# side holds the same password (in monitoring, both sides can name this one).
for c in vector grafana chargeback-aggregator federation-gateway federation-reconciler; do
  kubectl -n monitoring create secret generic "victorialogs-$c" \
    --from-literal=password="$(openssl rand -hex 32)"
done
helm install victorialogs ./helm/victorialogs -n monitoring \
  --set persistence.size=20Gi \
  --set retentionPeriod=60d \
  -f my-consumers.yaml   # auth.consumers[] copied from values.yaml (paths + from),
                         # each with existingSecret: victorialogs-<name>
```

For chart smoke-tests / CI you can disable the PVC (and skip the Secrets):

```bash
helm template ./helm/victorialogs --set persistence.enabled=false --set auth.mode=observe
```

(`emptyDir` mode — data evaporates with the pod. A `NOTES.txt` warning fires.)

## Compliance branch (Phase 3)

VictoriaLogs (like Loki) is **not tamper-evident / WORM**. If strict compliance (legal hold, immutable retention) arrives, the Vector chart fans out to an external SIEM — see [#539 §3 / §4 Phase 3](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539). This chart is unchanged in that branch; the SIEM hangs off the shipper, not the store.
