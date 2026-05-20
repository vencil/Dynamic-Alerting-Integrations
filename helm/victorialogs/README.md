# victorialogs Helm chart

Central log store for the platform log-aggregation pipeline ([#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539) Phase 1).

Built on **VictoriaLogs** (`victoriametrics/victoria-logs:v1.50.0`) — single binary, no object-storage ring / compactor / memcached. The operational-simplicity choice over Loki / ELK is recorded in the source issue §3.

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

## Install

```bash
helm install victorialogs ./helm/victorialogs -n monitoring \
  --set persistence.size=20Gi \
  --set retentionPeriod=60d
```

For chart smoke-tests / CI you can disable the PVC:

```bash
helm template ./helm/victorialogs --set persistence.enabled=false
```

(`emptyDir` mode — data evaporates with the pod. A `NOTES.txt` warning fires.)

## Compliance branch (Phase 3)

VictoriaLogs (like Loki) is **not tamper-evident / WORM**. If strict compliance (legal hold, immutable retention) arrives, the Vector chart fans out to an external SIEM — see [#539 §3 / §4 Phase 3](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539). This chart is unchanged in that branch; the SIEM hangs off the shipper, not the store.
