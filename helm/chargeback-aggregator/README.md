# chargeback-aggregator Helm chart

Daily federation-chargeback aggregator for the platform log-aggregation pipeline ([#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539) Phase 2 / [#552](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/552)).

CronJob queries VictoriaLogs for `log_type=prometheus_query_log` entries, aggregates per-tenant `samples_scanned` + `exec_time_s` totals over a 24h window, writes a CSV report to a PVC.

## Why this exists (the #552 boundary)

`tenant_federation_requests_total{tenant,status}` from the gateway mtail sidecar counts **requests**, not **cost**. A heavy 100k-series query and a light 10-series query both increment that counter by 1 — useless for chargeback. IV-2f (#511) deliberately did **not** add `series_returned` to the gateway audit log because Envoy would have to buffer + decode the Prometheus response body (cost prohibitive; blast-radius enforcement lives at the storage `--query.max-samples` cap instead).

The accurate cost signal is in Prometheus's own **query log** (`query_log_file` config setting — there's no CLI flag for this), which records per-query `stats.samples.totalQueryableSamples` + `stats.timings.evalTotalTime` + `stats.timings.execTotalTime`. This chart's CronJob aggregates those, offline, into a billable per-tenant report.

## Pipeline

```
Prometheus (query_log_file: /dev/stderr)
    │  one JSON line per PromQL query, includes the post-prom-label-proxy
    │  query string (containing {tenant_id="X"} for federation requests)
    ▼
Vector DaemonSet (additionalSources: prometheus_query_log)
    │  VRL: parse_json → if exists(.params.query) → log_type=prometheus_query_log
    │  extract tenant_id via regex; hoist samples_scanned / exec_time_s / eval_time_ms
    ▼
VictoriaLogs (same store as Phase 1)
    │  stream: {app, k8s_namespace, log_type, tenant_id}
    ▼
chargeback-aggregator (this chart, daily CronJob)
    │  LogsQL: stats by (tenant_id) sum(samples_scanned), sum(exec_time_s), count()
    ▼
chargeback-YYYY-MM-DD.csv on PVC
```

## Tenant attribution caveat

Rows whose `params.query` doesn't contain `{tenant_id="..."}` are bucketed as `tenant=platform` in the CSV. These come from:
- Recording / alerting rule evaluations (no tenant selector)
- Federation-proxy-internal probes
- Manual queries from operators

`platform` rows are **not billable** — they reflect the platform's own load. Useful for sanity-checking but should never appear on a customer invoice.

## Install

```bash
# After helm/victorialogs + helm/vector (with the prometheus_query_log
# additional source enabled) are running:
helm install chargeback ./helm/chargeback-aggregator -n monitoring

# Smoke-test: run once immediately + watch
helm upgrade chargeback ./helm/chargeback-aggregator -n monitoring \
  --reuse-values --set manualJob.enabled=true
kubectl logs -n monitoring -l app.kubernetes.io/name=chargeback-aggregator --tail=50
```

## Retention

`output.retentionDays: 90` by default (CSV files older than that are deleted at the start of each run). Bump for finance teams that keep longer billing-cycle records.

## What this chart deliberately does NOT do

- **Push metrics anywhere** — no pushgateway, no remote_write. Output is a file on a PVC. If you want Prometheus to see the totals, mount the same PVC into a small textfile-collector pod or wrap this in a follow-up chart. Kept simple here to avoid pulling in pushgateway as a dependency.
- **Compute money** — only raw cost dimensions (samples, exec time, query count). The per-unit pricing belongs to finance, not the platform.
- **Real-time aggregation** — by design (#552). A slow chargeback run must never wedge the federation hot path, so this lives in a CronJob, not a hot-path component.
