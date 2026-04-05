# tenant-api (v2.4.0)

> **Tenant Management REST API** — config-driven 的租戶管理服務，提供 CRUD + Batch 操作、RBAC 權限控制、GitOps commit-on-write 審計軌跡。搭配 oauth2-proxy sidecar 實現 IdP 整合認證。
>
> **其他文件：** [README](../../README.md) (概覽) · [ADR-009](../../docs/adr/009-tenant-manager-crud-api.md) (架構決策) · [Architecture & Design](../../docs/architecture-and-design.md) (技術深度) · [API Reference](../../docs/api/README.md) (API 參考)

## 架構

- **chi Router**: 輕量 HTTP router，搭配 RequestID / RealIP / Logger / Recoverer / Timeout middleware
- **RBAC**: `_rbac.yaml` 定義 group → tenant → permission 映射，`atomic.Value` hot-reload（預設 30s）
- **GitOps Writer**: schema validation → YAML 寫入 → `git commit`（以操作者 email 為 author）
- **Conflict Detection**: 寫入前檢查 git HEAD，若有其他變更返回 409 Conflict
- **Path Traversal 防護**: `ValidateTenantID()` 拒絕 `..`、`/`、`\` 等路徑穿越
- **Prometheus Metrics**: atomic counter 實現，零外部依賴（不需 prometheus/client_golang）
- **Security**: `ReadHeaderTimeout` (Gosec G112)、1 MB body limit、non-root container

## Endpoints

| Method | Path | 說明 | 權限 |
|--------|------|------|------|
| `GET` | `/health` | Liveness probe | 無需認證 |
| `GET` | `/ready` | Readiness probe（config-dir 可讀） | 無需認證 |
| `GET` | `/metrics` | Prometheus metrics（text exposition） | 無需認證 |
| `GET` | `/api/v1/tenants` | 列出所有租戶（含 silent/maintenance 狀態） | read |
| `GET` | `/api/v1/tenants/{id}` | 取得租戶完整設定（raw YAML + resolved thresholds） | read |
| `PUT` | `/api/v1/tenants/{id}` | 更新租戶設定（validate → write → git commit） | write |
| `POST` | `/api/v1/tenants/{id}/validate` | Dry-run 驗證（不寫入） | read |
| `POST` | `/api/v1/tenants/{id}/diff` | 預覽變更 diff（current vs proposed） | read |
| `POST` | `/api/v1/tenants/batch` | 批次操作（per-tenant 權限檢查） | read + per-tenant write |

## Metrics 輸出

```prometheus
# HELP tenant_api_up Whether the tenant-api is up.
# TYPE tenant_api_up gauge
tenant_api_up 1
# HELP tenant_api_uptime_seconds Seconds since tenant-api started.
# TYPE tenant_api_uptime_seconds gauge
tenant_api_uptime_seconds 3600.0
# HELP tenant_api_requests_total Total API requests.
# TYPE tenant_api_requests_total counter
tenant_api_requests_total 42
# HELP tenant_api_errors_total Total API errors.
# TYPE tenant_api_errors_total counter
tenant_api_errors_total 2
# HELP tenant_api_writes_total Total write operations (git commits).
# TYPE tenant_api_writes_total counter
tenant_api_writes_total 5
```

## RBAC 設定

透過 `_rbac.yaml` 定義群組權限：

```yaml
groups:
  - name: platform-admins
    tenants: ["*"]
    permissions: [read, write, admin]

  - name: db-operators
    tenants: ["db-a-*", "db-b-*"]
    permissions: [read, write]

  - name: viewers
    tenants: ["*"]
    permissions: [read]
```

認證由 oauth2-proxy sidecar 處理，將 `X-Forwarded-Email` / `X-Forwarded-Groups` 注入後端請求 header。

## K8s 部署

### Helm（推薦）

```bash
# 安裝
helm install tenant-api \
  oci://ghcr.io/vencil/charts/tenant-api --version 2.4.0 \
  -n monitoring --create-namespace \
  -f values-override.yaml

# 升級
helm upgrade tenant-api \
  oci://ghcr.io/vencil/charts/tenant-api --version 2.4.0 \
  -n monitoring -f values-override.yaml
```

> **已 clone 專案？** 也可指向本地 chart 目錄：
> ```bash
> helm install tenant-api ./helm/tenant-api \
>   -n monitoring --create-namespace -f values-override.yaml
> ```

Helm chart 會自動建立：Deployment + oauth2-proxy sidecar、Service (80 → oauth2-proxy, 8080 → tenant-api)、RBAC ConfigMap、NetworkPolicy、PDB。

### Docker

```bash
# 從 repo root 建構（因 go.mod replace directive 需要 threshold-exporter 模組）
docker build -t ghcr.io/vencil/tenant-api:2.4.0 \
  -f components/tenant-api/Dockerfile .

# 執行
docker run -p 8080:8080 -v $(pwd)/conf.d:/conf.d \
  ghcr.io/vencil/tenant-api:2.4.0
```

### 驗證部署

```bash
# Pod 狀態
kubectl get pods -n monitoring -l app=tenant-api

# Health check
curl -s http://localhost:8080/health

# 列出租戶
curl -s http://localhost:8080/api/v1/tenants | python3 -m json.tool

# Prometheus metrics
curl -s http://localhost:8080/metrics
```

## 環境變數

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `TA_CONFIG_DIR` | `/conf.d` | 租戶 YAML 檔案目錄 |
| `TA_GIT_DIR` | (同 config-dir) | Git repository root |
| `TA_RBAC_PATH` | (空 = open-read) | `_rbac.yaml` 路徑 |
| `TA_ADDR` | `:8080` | HTTP listen address |

## 開發

```bash
# 執行測試（99 個 Go 測試）
cd components/tenant-api && go test ./...

# Lint
cd components/tenant-api && golangci-lint run

# Build binary
cd components/tenant-api && go build -o tenant-api ./cmd/server
```

## 版號策略

| Tag 格式 | 產出 | 說明 |
|----------|------|------|
| `tenant-api/v*` | `ghcr.io/vencil/tenant-api` Docker image + Helm chart | tenant-api 專屬版號線 |
| `exporter/v*` | threshold-exporter + Helm chart | — |
| `tools/v*` | da-tools CLI image | — |
| `portal/v*` | da-portal image | — |
| `v*` | Platform tag (GitHub Release) | 不觸發 build |
