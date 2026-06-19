# tenant-api

> 多租戶 alerting 平台的**配置寫入 / 讀取 API**:RBAC 過濾的 CRUD、批次、async task、SSE 事件流,寫入會落成真實 git commit 或可審查 PR/MR——**零資料庫**。
>
> *GitOps-native, RBAC-scoped tenant config API. Writes become Git commits / reviewable PRs. No database.*

## 給誰看

這份是**元件層的技術文件**,依**文件型別**組織(不依組織角色——角色路徑在 [`docs/getting-started/`](../../docs/getting-started/)):

| 你是… | 看這裡 |
|-------|--------|
| **平台工程師 / 維運**(部署、設定 RBAC / 寫回 / 聯邦、接 oauth2-proxy) | 本 README(參考) + [QUICKSTART](QUICKSTART.md)(5 分鐘跑起來) |
| **API 整合 / 自動化**(CI 寫 config、拉 federation token、串 SSE) | 下方 [API 參考](#api-參考) + [QUICKSTART 的整合路徑](QUICKSTART.md) |
| **想自助改告警的租戶 / 領域專家**(人類,用 UI) | 不需碰這個 API → 用 **Tenant Manager portal**;入門見 [租戶指南](../../docs/getting-started/for-tenants.md) |

> 💡 **想先把它跑起來?** → **[QUICKSTART.md](QUICKSTART.md)**(`docker run` + `curl /me`,看到零 DB 的 RBAC 身分)。本篇是完整 endpoint **參考**。
>
> 認證不在這裡做——身分由前置的 **oauth2-proxy**(處理 SSO 登入的前置 proxy)sidecar 注入 header,本服務信任它。

## 這個服務做什麼 / 不做什麼

**做**

- 租戶 / 群組 / saved view 的 CRUD、批次操作、effective config 解析(含繼承來源鏈)
- 租戶**自助宣告式告警**(Custom Alerts):租戶從平台提供的參數化 recipe 產生合法告警,**不需寫 PromQL**(人類走 portal,自動化走 API)
- 寫入時做 schema 驗證、domain policy 檢查,再以 git commit-on-write 或開 PR/MR 寫回
- 以 RBAC 對租戶列表 / 群組成員 / pending PR / async task 結果做**逐呼叫者**過濾
- 租戶聯邦:簽發短效 token 供租戶拉取自己的 metrics 子集、管理聯邦白名單與每租戶子集
- 維運硬化面:逐呼叫者限流、`X-Request-ID` 回拋、request body 大小與內容範圍驗證

**不做**

- **不做認證**——身分來自 oauth2-proxy 注入的 `X-Forwarded-Email` / `X-Forwarded-Groups`
- **不做 schema 演化**——YAML schema 由 threshold-exporter 的 config 套件擁有
- **不做持久化 task store**——async task 存在記憶體,pod 重啟後消失(polling 收到 404 視為 task 遺失)。唯一的跨 replica 持久狀態是聯邦 token 記錄(存於共用 ConfigMap,非資料庫)
- **不做 PR / MR 合併**——建立後等人工 review,只追蹤狀態

## 架構速覽

- **chi router** + 標準 middleware 鏈(RequestID / RealIP / Logger / Recoverer / Timeout)
- **`X-Request-ID` 回拋**:把 request id 寫回 response header,方便對應後端 log
- **逐呼叫者限流**:sliding-window,預設 100 req/min/caller,可關閉
- **RBAC**:`_rbac.yaml` 定義 group → tenant → permission,熱重載(預設 30s);缺檔則進入 open-read 模式
- **逐租戶授權**:群組 / view / batch / PR 列表 / task 結果的每個成員都再過一次 per-tenant RBAC
- **GitOps Writer**:schema 驗證 → 寫 YAML → `git commit`(operator email 當 author,service account 當 committer)
- **衝突偵測**:寫入後比對 commit 的 parent 與寫入前的 HEAD;若期間被外部 commit 移動 → 回 409
- **內容範圍驗證**:固定欄位走 struct validator,`Patch` / `Filters` map 走逐 key 規則,違反一次回完整清單
- **Domain policy**:寫入前檢查租戶的 domain 規則,違反回 403
- **Async task**:worker goroutine 池跑批次,`/api/v1/tasks/{id}` polling
- **SSE 事件**:寫入成功後廣播 `config_change`,讓 UI 即時更新
- **Path traversal 防護**:租戶 id 驗證拒絕 `..`、`/`、`\`
- **自帶輕量 Prometheus metrics**(無額外 client 函式庫依賴)、安全預設(header read timeout、1 MB body 上限、non-root 容器)

## API 參考

> 慣例:除 Health / Identity / Metrics 外皆需身分(oauth2-proxy header)。「權限」欄為該端點要求的 RBAC 動作;標「逐租戶」者會對每個受影響租戶再驗一次。

### Health / Identity / Metrics(無需認證)

| Method | Path | 說明 |
|--------|------|------|
| `GET` | `/health` | Liveness——永遠 200 |
| `GET` | `/ready` | Readiness——config 目錄無法存取時回 503 |
| `GET` | `/metrics` | Prometheus 文字格式 |

### 租戶配置

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/me` | read | 當前呼叫者的 email + groups + RBAC 摘要 |
| `GET` | `/api/v1/tenants` | read | 列出 RBAC 可見的租戶 |
| `GET` | `/api/v1/tenants/search` | read | 伺服端 search / filter / 分頁(`q` / `environment` / `tier` / `domain` / `db_type` / `tag` / `page_size` / `offset` / `sort`);內含短期快照快取,為大量租戶下的低延遲設計 |
| `GET` | `/api/v1/tenants/{id}` | read | 取得 raw YAML + 解析後的閾值 |
| `GET` | `/api/v1/tenants/{id}/effective` | read | 最終生效設定(租戶覆寫與平台預設逐層合併後的值)+ 繼承來源鏈 + 雙重 hash(`source_hash` / `merged_hash`,供變更偵測) |
| `GET` | `/api/v1/tenants/{id}/access` | read | 輕量 RBAC 授權探測:可讀該租戶回 `200 {allow,tenant,permission}`、否則 `403`。供姊妹服務(如 recipe-preview #657)重用 tenant-isolation 決策、不重寫 RBAC 也不過度取得設定 |
| `PUT` | `/api/v1/tenants/{id}` | write | 寫入(驗證 → policy → 寫入 → commit / PR);body 格式錯誤回 400 |
| `POST` | `/api/v1/tenants/{id}/validate` | read | Dry-run 驗證,不寫入 |
| `POST` | `/api/v1/tenants/{id}/diff` | read | 預覽 unified diff |
| `POST` | `/api/v1/tenants/batch` | read + 逐租戶 write | 批次套用(逐筆 RBAC + policy;`?async=true` 走 task 池) |

> **寫入回應**:`PUT /{id}` 回 `{"status","tenant_id"}`;PR 模式另含 `pr_url` / `pr_number`(CI 可據此取得待審 PR)。request body 直接送租戶 YAML,不需特定 `Content-Type`。

### Custom Alerts(租戶自助告警)

租戶從平台提供的**參數化 recipe** 產生告警(免寫 PromQL)。**人類請用 Tenant Manager portal 的 RecipeBuilder**(選 recipe、填參數、一鍵 commit);以下端點供**自動化 / 整合**直接呼叫。

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/tenants/{id}/metrics` | read | Metric 探索:回傳該租戶近期出現的 metric 名稱,供 RecipeBuilder 選取(伺服端強制鎖該租戶 label) |
| `PUT` | `/api/v1/tenants/{id}/custom-alerts` | write | 寫入該租戶的 custom-alert recipe 集合(驗證後 commit / PR 回 GitOps) |

### 群組 / View

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/groups` | read | 列出群組(自動隱藏成員全不可讀的群組) |
| `GET` | `/api/v1/groups/{id}` | read | 取得群組 |
| `PUT` | `/api/v1/groups/{id}` | write + 逐成員 write | 寫入;對所有 `members` 都需 write,否則回 403 + 不足清單 |
| `DELETE` | `/api/v1/groups/{id}` | write + 逐成員 write | 刪除(同上權限) |
| `POST` | `/api/v1/groups/{id}/batch` | read + 逐成員 write | 對群組全成員套 patch(同步 / async) |
| `GET` | `/api/v1/views` | read | 列出 saved view |
| `GET` `PUT` `DELETE` | `/api/v1/views/{id}` | read / write | Saved view CRUD |

### Async / 事件

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/tasks/{id}` | read | Async task polling;結果以呼叫者 RBAC 過濾,全不可讀回 403 |
| `GET` | `/api/v1/prs` | read | Pending PR / MR 列表;不可讀的租戶自動隱藏,`?tenant=<id>` 不可讀回空陣列 |
| `GET` | `/api/v1/events` | read | SSE 即時事件流(`config_change`) |

### 聯邦(Federation)

讓租戶安全拉取**自己的** metrics 子集:平台維護白名單與每租戶子集,並簽發短效 token 供租戶向 read-path proxy 取數。

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/federation/policy` | admin | 取得平台聯邦白名單 |
| `PUT` | `/api/v1/federation/policy` | admin | 更新白名單(新增 metric 會跑資料層 admission 檢查;軟性警告需 `force=true` + 理由才放行,並記入 commit) |
| `POST` | `/api/v1/federation/tokens` | admin(對 body 的租戶) | 簽發短效 token(預設 4h);token 本體只在回應出現一次 |
| `GET` | `/api/v1/federation/tokens?tenant_id=<id>` | admin(對該租戶) | 列出該租戶未過期的 token 記錄(不含 token 本體) |
| `DELETE` | `/api/v1/federation/tokens/{id}` | admin(對該 token 的租戶) | 撤銷 token;最終一致,約 1–2 分鐘內隨設定同步生效 |
| `GET` | `/api/v1/tenants/{id}/federation` | read | 取得該租戶的聯邦 metric 子集 |
| `PUT` | `/api/v1/tenants/{id}/federation` | admin | 更新該租戶的聯邦 metric 子集(需該租戶 admin;子集不得超出平台白名單) |

> token 記錄存於跨 replica 共用的 Kubernetes ConfigMap(由 Helm chart 預建),服務維持 stateless、可多 replica。濫用防線:每租戶同時最多 16 個有效 token + 每分鐘簽發上限,超出分別回 409 / 429。未設定簽章金鑰時,整組聯邦 token 端點不註冊。

## 維運

### 限制與上限

| 項目 | 預設 | 可調 |
|------|------|------|
| 逐呼叫者限流 | 100 req/min | `TA_RATE_LIMIT_PER_MIN`(`0` 關閉) |
| Request body | 1 MB | `TA_MAX_BODY_BYTES` |
| 批次操作數 | 1–1000 / 次 | — |
| Search page_size | 1–500(預設 50) | — |
| Patch key / value 長度 | ≤ 256 / ≤ 1024 字元 | — |

### 限流回應格式

```json
{ "error": "rate limit exceeded for user@example.com; try again in 12s",
  "code": "RATE_LIMITED", "retry_after_s": 12 }
```

同步輸出 `Retry-After` header。`/health` / `/ready` / `/metrics` 永遠不限流。

### 衝突語義

寫入時記錄 git HEAD;若 commit 的 parent 與寫入前 HEAD 不符(期間有外部 commit 落地)→ 回 409,呼叫者應 refresh 後重試。

### Open-read 模式

未配置 `_rbac.yaml` 時所有讀寫端點放行(僅守 path traversal)。**僅供單人 dev,切勿上 production。**

## 可觀測性

### Metrics(`/metrics`)

```prometheus
tenant_api_up 1
tenant_api_uptime_seconds 3600.0
tenant_api_requests_total 42
tenant_api_errors_total 2
tenant_api_writes_total 5
tenant_api_rate_limit_rejections_total 3
tenant_api_rate_limit_active_callers 12
```

`rate_limit_rejections_total` 為程序啟動以來被限流擋下的請求數;`rate_limit_active_callers` 為滾動視窗內仍活躍的呼叫者數(背景 sweeper 控管記憶體)。

### Request 對應

每筆 request 回 `X-Request-ID`(自動產生或沿用客戶傳入)。後端用結構化 JSON log 輸出,每行帶 `request_id`,5xx 升為 WARN;回報問題時附上此 id 即可 grep 後端 log。`TA_LOG_LEVEL` 控制 verbosity。

### SSE 事件

```text
event: config_change
data: {"type":"config_change","tenant_id":"db-a-prod","timestamp":"2026-05-03T10:00:00Z","detail":"tenant config updated"}
```

## 設定

### 環境變數

| 變數 | 預設 | 說明 |
|------|------|------|
| `TA_CONFIG_DIR` | `/conf.d` | 租戶 YAML 目錄 |
| `TA_GIT_DIR` | (同 config dir) | Git repository 根目錄 |
| `TA_RBAC_PATH` | (空 = open-read) | `_rbac.yaml` 路徑 |
| `TA_ADDR` | `:8080` | HTTP listen address |
| `TA_RATE_LIMIT_PER_MIN` | `100` | 逐呼叫者限流;`0` 關閉;非整數值回退預設並印 WARN |
| `TA_MAX_BODY_BYTES` | `1048576` | request body 上限(bytes) |
| `TA_READ_TIMEOUT` / `TA_WRITE_TIMEOUT` / `TA_IDLE_TIMEOUT` | `15s` / `30s` / `60s` | HTTP server timeout(大批次 + 慢 git push 時可調高 write timeout) |
| `TA_LOG_LEVEL` | `info` | `debug` / `info` / `warn` / `error` |
| `TA_WRITE_MODE` | `direct` | `direct` / `pr` / `pr-github` / `pr-gitlab` |
| `TA_GITHUB_TOKEN` / `TA_GITHUB_REPO` / `TA_GITHUB_BASE_BRANCH` / `TA_GITHUB_API_URL` | (空) | GitHub PR 模式;repo 為 `owner/repo`,API URL 供 Enterprise |
| `TA_GITLAB_TOKEN` / `TA_GITLAB_PROJECT` / `TA_GITLAB_TARGET_BRANCH` / `TA_GITLAB_API_URL` | (空) | GitLab MR 模式;project 為 `group/project` 或數字 ID,API URL 供自託管 |
| `GIT_COMMITTER_NAME` / `GIT_COMMITTER_EMAIL` | (空) | service account 身分;空時 fallback 到 author |
| `TA_FEDERATION_KEY` | (空 = 停用) | 簽發聯邦 token 的私鑰 PEM 路徑;空則聯邦 token 端點不註冊 |
| `TA_FEDERATION_STORE` | `tenant-federation-store` | 存放聯邦 token 記錄的 ConfigMap 名稱(Helm chart 預建) |
| `TA_FEDERATION_NAMESPACE` | (空 = pod 自身 namespace) | 上述 ConfigMap 所在 namespace |
| `TA_FEDERATION_TOKEN_TTL` | `4h` | 聯邦 token 效期 |

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

支援以環境 / 域 metadata 做進一步過濾(細節見 `internal/rbac/` 註解)。

## 寫回模式

| 模式 | 行為 | 適用 |
|------|------|------|
| `direct` | 直接 `git commit` | dev、單人操作 |
| `pr` / `pr-github` | 建 feature branch + GitHub PR | GitHub.com / Enterprise |
| `pr-gitlab` | 建 feature branch + GitLab MR | GitLab.com / 自託管 |

PR 模式啟動時會驗證 token + 連線;失敗只印 WARN(後續開 PR 會回 503)。範例:

```bash
# GitHub Enterprise
export TA_WRITE_MODE=pr-github
export TA_GITHUB_TOKEN=...                # 需 contents:write + pull_requests:write
export TA_GITHUB_REPO=org/config-repo
export TA_GITHUB_API_URL=https://github.internal.example.com/api/v3

# GitLab 自託管
export TA_WRITE_MODE=pr-gitlab
export TA_GITLAB_TOKEN=...                # 需 api scope
export TA_GITLAB_PROJECT=infra/alerting-config
export TA_GITLAB_API_URL=https://gitlab.internal.example.com
```

## 部署

> Kubernetes 上的完整導引(Helm values、oauth2-proxy、PR 寫回、HA)以 **[平台工程師指南 §部署 tenant-api](../../docs/getting-started/for-platform-engineers.md)** 為準,本節只給最小指令。

```bash
# Helm（版本見 Releases / CHANGELOG；省略 --version 取最新，或 --version <x.y.z> 釘版）
helm install tenant-api oci://ghcr.io/vencil/charts/tenant-api \
  -n monitoring --create-namespace -f values-override.yaml
# 或指向本地 chart：helm install tenant-api ./helm/tenant-api -n monitoring -f values-override.yaml
```

Chart 會建立:Deployment + oauth2-proxy sidecar、Service、RBAC ConfigMap、NetworkPolicy、PDB。

本機 Docker(從 repo root build,因 go.mod 需 threshold-exporter 模組):

```bash
docker build -t tenant-api -f components/tenant-api/Dockerfile .
docker run -p 8080:8080 -v "$(pwd)/conf.d:/conf.d" tenant-api
# 或直接拉 published image（<version> 見 Releases / CHANGELOG）：
#   docker run -p 8080:8080 -v "$(pwd)/conf.d:/conf.d" ghcr.io/vencil/tenant-api:<version>
```

Smoke test:

```bash
curl -s localhost:8080/health
curl -s localhost:8080/metrics
```

## 開發

```bash
go test ./... -race          # 全測試 + race detector
golangci-lint run            # lint
go build -o tenant-api ./cmd/server
```

PR 合併前以 repo 層 `make pr-preflight` 統一把關。

## 延伸閱讀

- **跑起來**:[QUICKSTART.md](QUICKSTART.md) ·  **整套體驗**:[`try-local/`](../../try-local/)(portal 改 config → 本服務 commit → exporter 熱重載 → 告警)
- **角色指南**:[平台工程師](../../docs/getting-started/for-platform-engineers.md) · [領域專家](../../docs/getting-started/for-domain-experts.md) · [租戶](../../docs/getting-started/for-tenants.md)
- **版本歷程**:[CHANGELOG.md](../../CHANGELOG.md) · 版號線 `tenant-api/v*` → `ghcr.io/vencil/tenant-api` image + Helm chart
- **設計與 API 深度**:[架構與設計](../../docs/architecture-and-design.md) · [API 文件](../../docs/api/README.md)
