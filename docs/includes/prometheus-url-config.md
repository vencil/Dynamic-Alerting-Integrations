> **Prometheus 連線設定：**
> | 環境 | URL |
> |------|-----|
> | K8s 內部 | `http://prometheus.monitoring.svc.cluster.local:9090` |
> | Docker Desktop | `http://host.docker.internal:9090` |
> | Linux Docker (--network=host) | `http://localhost:9090` |
>
> 透過 `--prometheus <URL>` flag 或 `PROMETHEUS_URL` 環境變數設定。
