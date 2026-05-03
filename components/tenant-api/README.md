# tenant-api (v2.8.0)

> **Tenant Management REST API** — config-driven 多租戶 alerting 平台的後端寫入 + 讀取面，提供 RBAC 過濾後的 CRUD、批次操作、async task polling、SSE 事件流，以及 `direct` / GitHub PR / GitLab MR 三種寫回模式。認證由 oauth2-proxy sidecar 處理。

## What this service does (and doesn't)

**Does**

- 暴露租戶 / 群組 / saved view 的 CRUD、批次操作、effective config 解析
- 寫入時做 schema validation、domain policy 檢查、git commit-on-write 或 PR/MR 寫回
- 套用 RBAC 對 tenant 列表 / group 成員 / pending PR / async task 做 per-caller filtering
- per-caller rate limit、`X-Request-ID` echo、body-content range validation 等 v2.8.0 hardening surface

**Doesn't**

- **不做認證**：身份來自 oauth2-proxy 注入的 `X-Forwarded-Email` / `X-Forwarded-Groups` header
- **不做 schema 演化**：YAML schema 由 `pkg/config`（threshold-exporter）擁有
- **不做持久化 task store**：async task 是 in-memory，pod restart 後資料消失（client polling 收到 404 時應視為 task lost）
- **不做 PR / MR 合併**：建立 PR/MR 後等待人工 review，只追蹤狀態

## Architecture

- **chi Router** + 標準 middleware 鏈（RequestID / RealIP / Logger / Recoverer / Timeout）
- **`X-Request-ID` echo**：chi 注入的 request id 寫回 response header，方便 client 對 log
- **Per-caller rate limit**：sliding-window，預設 100 req/min/caller，`TA_RATE_LIMIT_PER_MIN=0` 關閉
- **RBAC**：`_rbac.yaml` 定義 group → tenant → permission，`atomic.Value` hot-reload（預設 30s）；缺少 `_rbac.yaml` 進入 open-read mode
- **Tenant-scoped authz**（v2.8.0 B-6 PR-2）：group / view / batch / PR list / task results 的成員都會被 per-tenant RBAC 再過一次
- **GitOps Writer**：schema validation → YAML 寫入 → `git commit`（operator email 為 author，service account 為 committer）
- **Conflict detection**：寫入後檢查 commit 的 parent；若 HEAD 在 read 跟 write 之間移動回 409
- **Body-content validation**（issue #134）：fixed-shape 欄位走 go-playground/validator，`Patch` / `Filters` map 走 per-key registry，違反一次回完整 violations 陣列
- **Domain policy**：寫入前檢查 `_domain_policy.yaml` 規則，違反回 403
- **Async tasks**：4 worker goroutine 跑批次操作，`/api/v1/tasks/{id}` polling
- **SSE event hub**：寫入成功後廣播 `config_change` 事件，UI 即時更新
- **Path traversal 防護**：`ValidateTenantID()` 拒絕 `..`、`/`、`\`
- **Prometheus metrics**：atomic counter 自實作，無 prometheus/client_golang 依賴
- **Security**：`ReadHeaderTimeout` (Gosec G112)、1 MB body limit、non-root container

## API reference

### Health / Identity / Metrics（無需認證）

| Method | Path | 說明 |
|--------|------|------|
| `GET` | `/health` | Liveness probe — always 200 |
| `GET` | `/ready` | Readiness — 503 if `configDir` 無法 stat |
| `GET` | `/metrics` | Prometheus text exposition |

### Tenant

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/me` | read | 回傳當前 caller 的 email + groups + RBAC 摘要 |
| `GET` | `/api/v1/tenants` | read | 列出 RBAC 可見的租戶（含 silent / maintenance / `_metadata`） |
| `GET` | `/api/v1/tenants/search` | read | **(v2.8.0 C-1)** 伺服端 search / filter / pagination；`q` / `environment` / `tier` / `domain` / `db_type` / `tag` / `page_size`（max 500）/ `offset` / `sort`。內含 30s snapshot cache，目標 1000 租戶下 p99 < 200ms。⚠️ numeric offset 仍是 v1 design — opaque cursor 未來換 |
| `GET` | `/api/v1/tenants/{id}` | read | 取得 raw YAML + resolved thresholds |
| `GET` | `/api/v1/tenants/{id}/effective` | read | **(v2.7.0 B-3)** merged config + 來源鏈 + dual hashes（`source_hash` + `merged_hash`，16 hex），底層走 `pkg/config/hierarchy.ResolveEffective()`，ADR-018 L0→L3 繼承語義 |
| `PUT` | `/api/v1/tenants/{id}` | write | 寫入（validate → policy check → write → commit / PR） |
| `POST` | `/api/v1/tenants/{id}/validate` | read | Dry-run 驗證 |
| `POST` | `/api/v1/tenants/{id}/diff` | read | 預覽 unified diff |
| `POST` | `/api/v1/tenants/batch` | read + per-tenant write | 批次（per-op RBAC + policy 檢查；`?async=true` 走 task pool） |

### Group / View

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/groups` | read | 列出 group（過濾掉成員都不可讀的 group） |
| `GET` | `/api/v1/groups/{id}` | read | 取得 group |
| `PUT` | `/api/v1/groups/{id}` | write + per-member write | 寫入；caller 對所有 `members` 都需 PermWrite，否則 403 + forbidden 列表 |
| `DELETE` | `/api/v1/groups/{id}` | write + per-member write | 刪除；同上權限模型 |
| `POST` | `/api/v1/groups/{id}/batch` | read + per-member write | 對 group 全成員套 patch（同步 / async） |
| `GET` | `/api/v1/views` | read | 列出 saved view |
| `GET` `PUT` `DELETE` | `/api/v1/views/{id}` | read / write | Saved view CRUD |

### Async / Eventing

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/tasks/{id}` | read | Async task polling；`Results` 會被 caller-RBAC 過濾，全部不可讀回 403 |
| `GET` | `/api/v1/prs` | read | Pending PR / MR 列表；caller 不可讀的 `tenant_id` 自動隱藏；`?tenant=<id>` 不可讀回空陣列（避免 existence oracle） |
| `GET` | `/api/v1/events` | read | SSE 即時事件流（config_change） |

## Operational concerns

### Limits + caps

| 項目 | 值 | 來源 |
|------|----|------|
| Per-caller rate limit | 100 req/min（預設） | `TA_RATE_LIMIT_PER_MIN` |
| Request body | 1 MB（PUT / POST） | `io.LimitReader` |
| Batch operations | 1–1000 ops | struct-tag validator |
| Search page_size | 1–500（預設 50） | `tenant_search.go` |
| Tenant snapshot cache TTL | 30s | `tenant_search.go::snapshotTTL` |
| Patch key length | ≤ 256 chars | `body_validator.go::maxPatchKeyLen` |
| Patch value length | ≤ 1024 chars | `body_validator.go::maxPatchValueLen` |
| `_timeout_ms` value | ≤ 3,600,000（1 hr） | `body_validator.go` |

### Rate-limit response shape

```json
{
  "error":         "rate limit exceeded for user@example.com; try again in 12s",
  "code":          "RATE_LIMITED",
  "retry_after_s": 12
}
```

`Retry-After` header 同步輸出。`/health` / `/ready` / `/metrics` 永遠跳過限流。

### Conflict semantics

寫入時記錄 git HEAD before / after；若 commit 的 parent 跟 before 不符（外部 commit 落地）回 `ErrConflict` → 409。Client 應 refresh 後重試。

### Open-read mode

未配置 `_rbac.yaml` 時：所有 read 端點放行，所有 write 端點放行（僅 `ValidateTenantID` 守 path traversal）。**不適合 production**，僅給單人 dev 環境使用。

## Observability

### Metrics（`/metrics`）

```prometheus
tenant_api_up 1
tenant_api_uptime_seconds 3600.0
tenant_api_requests_total 42
tenant_api_errors_total 2
tenant_api_writes_total 5
```

### Request correlation

每筆 request 收到 `X-Request-ID` response header（chi 自動產或客戶傳入）。Server log 同步印出該 request id，客戶報問題時直接給 id 即可定位。

### SSE events

`GET /api/v1/events` 訂閱即時 config 變更：

```
event: config_change
data: {"type":"config_change","tenant_id":"db-a-prod","timestamp":"2026-05-03T10:00:00Z","detail":"tenant config updated"}
```

## Configuration

### Environment variables

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `TA_CONFIG_DIR` | `/conf.d` | 租戶 YAML 檔案目錄 |
| `TA_GIT_DIR` | (同 config-dir) | Git repository root |
| `TA_RBAC_PATH` | (空 = open-read) | `_rbac.yaml` 路徑 |
| `TA_ADDR` | `:8080` | HTTP listen address |
| `TA_RATE_LIMIT_PER_MIN` | `100` | Per-caller rate limit；`0` 關閉；malformed 值回退預設並印 WARN |
| `TA_WRITE_MODE` | `direct` | `direct` / `pr` / `pr-github` / `pr-gitlab` |
| `TA_GITHUB_TOKEN` | (空) | `pr-github` 必填 |
| `TA_GITHUB_REPO` | (空) | `owner/repo` |
| `TA_GITHUB_BASE_BRANCH` | `main` | PR target |
| `TA_GITHUB_API_URL` | (空) | GitHub Enterprise API URL |
| `TA_GITLAB_TOKEN` | (空) | `pr-gitlab` 必填 |
| `TA_GITLAB_PROJECT` | (空) | `group/project` 或 numeric ID |
| `TA_GITLAB_TARGET_BRANCH` | `main` | MR target |
| `TA_GITLAB_API_URL` | (空) | 自託管 GitLab URL |
| `GIT_COMMITTER_NAME` / `GIT_COMMITTER_EMAIL` | (空) | service account 身份；空時 fallback 到 author |

### RBAC YAML

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

詳細 metadata-aware filtering（環境 / 域過濾）見 [rbac.go](internal/rbac/rbac.go) 註解。

## Write-back modes

| 模式 | 行為 | 適用場景 |
|------|------|----------|
| `direct` | 直接 `git commit` | dev、單人操作 |
| `pr` / `pr-github` | 建 feature branch + GitHub PR | GitHub.com / Enterprise Server |
| `pr-gitlab` | 建 feature branch + GitLab MR | GitLab.com / 自託管 |

兩種 PR 模式啟動時會跑 `ValidateToken()` 驗證 token + 連線；失敗印 WARN 但不 fatal（後續 PR 創建會 503）。

```bash
# GitHub Enterprise
export TA_WRITE_MODE=pr-github
export TA_GITHUB_TOKEN=ghp_xxx          # contents:write + pull_requests:write
export TA_GITHUB_REPO=org/config-repo
export TA_GITHUB_API_URL=https://github.internal.example.com/api/v3

# GitLab self-hosted
export TA_WRITE_MODE=pr-gitlab
export TA_GITLAB_TOKEN=glpat-xxx        # api scope
export TA_GITLAB_PROJECT=infra/alerting-config
export TA_GITLAB_API_URL=https://gitlab.internal.example.com
```

## Deployment

### Helm（建議）

```bash
helm install tenant-api \
  oci://ghcr.io/vencil/charts/tenant-api --version 2.8.0 \
  -n monitoring --create-namespace \
  -f values-override.yaml
```

或指向本地 chart：`helm install tenant-api ./helm/tenant-api -n monitoring -f values-override.yaml`

Chart 自動建立：Deployment + oauth2-proxy sidecar、Service（80 → oauth2-proxy、8080 → tenant-api）、RBAC ConfigMap、NetworkPolicy、PDB。

### Docker

```bash
# 從 repo root build（go.mod replace directive 需要 threshold-exporter 模組）
docker build -t ghcr.io/vencil/tenant-api:2.8.0 \
  -f components/tenant-api/Dockerfile .

docker run -p 8080:8080 -v $(pwd)/conf.d:/conf.d \
  ghcr.io/vencil/tenant-api:2.8.0
```

### Smoke test

```bash
kubectl get pods -n monitoring -l app=tenant-api
curl -s http://localhost:8080/health
curl -s http://localhost:8080/api/v1/tenants | python3 -m json.tool
curl -s http://localhost:8080/metrics
```

## Development

```bash
# 322 個 Go 測試（含 race detector）
go test ./... -race

# Lint
golangci-lint run

# Build
go build -o tenant-api ./cmd/server
```

Pre-commit hook (`.golangci.yml`) 與 repo 層 `make pr-preflight` 整合 — PR 合併前必跑。

## Versioning + references

- Tag line: `tenant-api/v*` → `ghcr.io/vencil/tenant-api` Docker image + Helm chart
- 版本歷程：[CHANGELOG.md](../../CHANGELOG.md)
- 架構決策：[ADR-009 commit-on-write CRUD](../../docs/adr/009-tenant-manager-crud-api.md)、[ADR-011 PR-based write-back](../../docs/adr/011-pr-based-writeback.md)
- 設計深度：[architecture-and-design.md](../../docs/architecture-and-design.md)
- API 細節：[docs/api/README.md](../../docs/api/README.md)、[tenant-api-hardening.md](../../docs/api/tenant-api-hardening.md)（v2.8.0 B-6 hardening 詳解）
- Repo overview：[../../README.md](../../README.md)
