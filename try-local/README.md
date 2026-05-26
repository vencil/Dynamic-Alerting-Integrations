# Try it locally — Dynamic Alerting in one command

> Spin up the whole platform on your laptop and watch a real alert fire. No Kubernetes, no cloud, no signup. / 一鍵在本機跑起整套平台，看著一個真實告警亮紅燈。

```bash
cd try-local
cp .env.example .env
docker compose up -d
```

> ℹ️ 首次啟動會從原始碼 **build tenant-api**（~1 分鐘）—— 它依賴的 `--dev-bypass-auth`（讓瀏覽器免 oauth2-proxy 也能登入）尚未發佈成 published image。之後的啟動會重用已 build 的 image；改了 tenant-api 原始碼後用 `docker compose up -d --build` 重建。其餘 3 個 component 用 published image。

Give it ~1 minute, then open:

| 產品 | 開這個 | 你會看到 |
|---|---|---|
| **Tenant Manager**（da-portal） | <http://localhost:8081> | 2 個 demo 租戶 + 一個預存的 Saved View；建立/儲存一個 Saved View 會落一個**真實 git commit** |
| **Tenant API**（tenant-api） | <http://localhost:8080/api/v1/me> | file-based 設定 API（commit-on-write），oauth2-proxy 身分模型 |
| **Prometheus** | <http://localhost:9090/alerts> | `MariaDBHighConnectionsCritical` **正在 firing**（紅燈） |
| **Alertmanager** | <http://localhost:9093> | 同一個 firing 告警，路由到 null receiver |

背後還有 **threshold-exporter**（把 config 變成 `user_threshold` 指標）和 **pushgateway**（裝載 seed 推進去的合成 DB 指標）。

驗證整條鏈是否正常：

```bash
make smoke-local      # 需要 curl + jq
```

完整清理（含匿名 volume，第二次啟動乾淨無殘留）：

```bash
make clean-local
```

---

## 兩種跑法

**① 完整 stack**（上面那個）— 6 個服務 + 2 個 one-shot seed，能看到 live 告警紅燈。

**② 只跑核心雙星**（Tenant Manager，不含監控）：

```bash
docker compose up da-portal tenant-api
```

只起 portal + tenant-api（外加一個秒退的 git-init one-shot）。打開 <http://localhost:8081>，瀏覽 2 個租戶、建立一個 Saved View 按 **Save** —— 然後在 host 上看 seed 設定 repo 多了一個真實 commit：

```bash
git -C try-local/seed/conf.d log --oneline
```

這就是「設定即 GitOps」最直接的體感：UI 一按 = 一個 commit。

## 看點

- **紅燈在哪**：告警狀態看 Prometheus `:9090/alerts` 和 Alertmanager `:9093` —— **portal 不顯示 live 告警**（它管設定，不管告警路由）。
- **為什麼會 fire**：seed 往 pushgateway 推了一筆 `mysql_global_status_threads_connected{tenant="db-demo"}=200`，超過 `db-demo` 設定的 critical 閾值 120 → DB rule pack 的 critical 規則 `for:30s` 後觸發。
- **設計概念展示**：第二個租戶 `cache-demo` 開了 silent_mode + severity dedup，會產生 v2.8.0 的 **Sentinel Alert / Severity Dedup** sentinel（`severity:none`，notification inhibit 來源）。

## 關於身分（dev-only auth bypass）

完整 production 由 oauth2-proxy 在前面注入 `X-Forwarded-Email` / `X-Forwarded-Groups`。try-local 沒有 oauth2-proxy，所以 tenant-api 用 `--dev-bypass-auth`（**僅限本機**）在缺 header 時注入一個 dev 身分（`dev@local` / `demo-admins`），瀏覽器才打得開 Tenant Manager。

> ⚠️ 這個 flag 嚴禁進 production：在 Kubernetes 內啟動會直接 panic，且 SAST 禁止它出現在任何部署 manifest。細節見 ADR-022。

`demo-admins` 在 `seed/conf.d/_rbac.yaml` 對應 `tenants: ["*"]`（這是 `/api/v1/me` 通過授權閘所必需的）。因此 `/api/v1/me` 回的 `accessible_tenants` 是 `["*"]`；想看到 2 個租戶清單請打 `GET /api/v1/tenants`（這也是 portal Tenant Manager 顯示的來源）。

## Port 衝突

所有 port 只綁 `127.0.0.1`（本機限定）—— dev-bypass 會為無 header 的請求注入 **admin** 身分，故刻意不對 LAN 開放（避免同網段他人取得寫入/commit 權）。要從別台裝置連，請自行改 compose 的 port binding。

預設用 8080 / 8081 / 9090 / 9091 / 9093。若被占用，編輯 `.env`（從 `.env.example` 複製來的）改任一 `EXPOSE_*_PORT` 後重啟：

```bash
make clean-local && docker compose up -d
```

## Windows / WSL2（必讀）

- Windows 使用者**必須**用 **WSL2 + Docker Desktop（WSL2 backend）**。
- **不要**從原生 Windows 路徑（`C:\...`）跑 —— bind mount 行為不保證。把 repo clone 到 **WSL2 檔案系統內**（如 `~/…`）再跑。
- 本 stack 多用匿名 volume；唯一 bind mount 是 `seed/conf.d`（read-write，用來承接 portal Save 的 git commit）。
- seed 會在 `seed/conf.d` 內 `git init`（runtime 產生的 `.git/` 已被 `.gitignore` 忽略）。Save 會改到這些被追蹤的 YAML —— `make clean-local` 後用 `git checkout try-local/seed/conf.d` 可還原 demo 初始狀態。

## 排錯

| 症狀 | 處理 |
|---|---|
| `docker compose up` 拉不到 image | image 在 `ghcr.io/vencil/*`；確認網路可達 ghcr.io。exporter/portal 目前是 amd64-only，Apple Silicon 會以 emulation 跑（較慢；原生多架構見 #463）。tenant-api 從源碼 build，為主機原生 arch。 |
| `:9090/alerts` 一直沒紅燈 | 給它 ~1–2 分鐘（recording rule 15s interval + 規則 `for:30s`）。仍沒有就 `docker compose logs seed-metrics prometheus`。 |
| 紅燈本來有、後來消失了 | pushgateway 是記憶體型（無持久化）—— 若單獨重啟它，seed 推的合成值會消失。重推：`docker compose up -d seed-metrics`。 |
| portal 開了但 API 502 | tenant-api 還在起；稍等。確認 `docker compose ps` 中 tenant-api 是 running。 |
| `make smoke-local` 說找不到 jq | 安裝 `jq`（smoke 需要 curl + jq）。 |

## 想試 da-tools（CLI）？

對 seed 設定跑一次護欄檢查（cardinality / schema / routing）：

```bash
docker run --rm -v "$PWD/seed/conf.d:/conf.d:ro" \
  ghcr.io/vencil/da-tools:${TOOLS_TAG:-v2.8.0} guard /conf.d
```

## Next Step：上 Production（Kubernetes）

try-local 跑順了、看對胃口 —— 下一步是評估正式部署到 Kubernetes：

- **Helm charts** → [`helm/`](../helm/)（`da-portal` / `tenant-api` / `threshold-exporter`；da-tools 是 CLI image，無 chart）
- **按角色入門** → [Platform Engineer 部署指南](../docs/getting-started/for-platform-engineers.md)
- **接上既有 Prometheus** → [BYO Prometheus](../docs/integration/byo-prometheus-integration.md) · [Prometheus Operator](../docs/integration/prometheus-operator-integration.md)

> ⚠️ try-local 用的 `--dev-bypass-auth` + `127.0.0.1`-only binding 是**本機限定**捷徑；production 改由 oauth2-proxy 注入身分、Helm values 控管（見 [ADR-022](../docs/adr/022-dev-auth-bypass-four-layer-containment.md)）。

