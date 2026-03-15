> **Prometheus Connection Settings:**
> | Environment | URL |
> |-------------|-----|
> | K8s Internal | `http://prometheus.monitoring.svc.cluster.local:9090` |
> | Docker Desktop | `http://host.docker.internal:9090` |
> | Linux Docker (--network=host) | `http://localhost:9090` |
>
> Configure via `--prometheus <URL>` flag or `PROMETHEUS_URL` environment variable.
>
> ⚠️ HTTP URLs above are for local development and in-cluster communication only. For production environments where Prometheus is exposed outside the cluster, use HTTPS with proper authentication (e.g., reverse proxy + mTLS).
