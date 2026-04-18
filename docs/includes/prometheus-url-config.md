<!-- Bilingual pair: prometheus-url-config.en.md -->

> **Prometheus 連線設定：**
> | 環境 | URL |
> |------|-----|
> | K8s 內部 | `http://prometheus.monitoring.svc.cluster.local:9090` |
> | Docker Desktop | `http://host.docker.internal:9090` |
> | Linux Docker (--network=host) | `http://localhost:9090` |
>
> 透過 `--prometheus <URL>` flag 或 `PROMETHEUS_URL` 環境變數設定。
>
> ⚠️ 以上 HTTP URL 僅適用於本地開發與叢集內部通訊。生產環境若 Prometheus 暴露於叢集外部，請改用 HTTPS 並搭配適當的認證機制（如 reverse proxy + mTLS）。
