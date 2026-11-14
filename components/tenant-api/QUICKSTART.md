# tenant-api — 5 分鐘跑起來

> 零資料庫的租戶配置 API:身分走 RBAC、寫入落成 git commit / 可審查 PR;每筆寫入 commit 前先由產生器容器判定([#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903))。
>
> *Zero-DB tenant config API — identity via RBAC, writes via Git, each write judged by the generator container first.*

| | |
|---|---|
| **是什麼** | GitOps 原生、RBAC-scoped 的租戶配置 API(commit-on-write / PR 寫回) |
| **給誰** | **平台工程師 / 維運**(部署)與 **API 整合 / 自動化**(直接呼叫)。<br>**人類租戶**改告警請用 **Tenant Manager portal**,不需碰這個 API。 |
| **前置** | Docker 20.10+、`curl`;Linux 主機另需 `git`、`python3` + PyYAML(本機跑產生器服務)。macOS／Windows 請改用 [try-local](../../try-local/README.md)(compose 已含產生器容器) |
| **你會看到** | `curl /api/v1/me` 從零 DB 回你的身分與 RBAC 權限(HTTP 200) |

> 版本:範例用本機 build 的 image。要拉 published image 時版本見 [Releases / CHANGELOG](../../CHANGELOG.md)。

---

## 路徑 A — 維運:把服務跑起來、確認身分模型

### 1) 準備一個 demo config 目錄(RBAC 規則 + 一個租戶)

```sh
# REPO：這個 repo checkout 的絕對路徑（專案根）。第 2 步的 build 與產生器服務都用它
REPO="/absolute/path/to/Dynamic-Alerting-Integrations"   # 改成你的 checkout
test -f "$REPO/scripts/tools/ops/generator_service.py" || echo "REPO 不是這個 repo 的根目錄"

mkdir -p demo/conf.d && cd demo

cat > conf.d/_rbac.yaml <<'EOF'
groups:
  - name: demo-admins
    tenants: ["*"]
    permissions: [read, write, admin]
EOF

cat > conf.d/db-a.yaml <<'EOF'
tenants:
  db-a:
    mysql_connection_usage_percent: "70"
EOF
```

### 2) 跑產生器服務與 tenant-api(零 DB;`TA_RBAC_PATH` 指向 `_rbac.yaml`,不設則所有存取都被拒絕;設成空字串會拒絕啟動)

tenant-api 每筆會改變 conf.d 內容的寫入,commit 前都問產生器服務(`da-tools generator-service`,經 Unix socket;[ADR-036](../../docs/adr/036-single-parser-effective-config.md)),沒給 `TA_GENERATOR_SOCKET` 就拒絕啟動。產生器只讀 git 物件,所以 conf.d 要是 git repo;兩邊必須同一個 uid(服務只回應同 uid 的對端)。

```sh
# conf.d 要是 git repo
git -C conf.d init -q && git -C conf.d add -A && \
  git -C conf.d -c user.email=dev@local -c user.name=dev commit -qm init

# 產生器服務($REPO 見第 1 步)。它判定時要跑三支 Go 讀取程式;任一支沒跑成
# (找不到、跑不起來、被訊號終止、太舊)時那筆寫入回 503(tool_fault)。
# 例外:在判定的記憶體上限下由 Go 執行期因記憶體不足自己結束的,是那筆寫入
# 太大,回 200 rejected(memory),不是 503。
make -s -C "$REPO" da-guard-build da-read-build da-crdecode-build
export DA_GUARD_BINARY="$REPO/.build/da-guard" DA_READ_BINARY="$REPO/.build/da-read" \
       DA_CRDECODE_BINARY="$REPO/.build/da-crdecode"
mkdir -p gen state
python3 "$REPO/scripts/tools/ops/generator_service.py" --socket "$(pwd)/gen/generator.sock" \
  --repo "$(pwd)/conf.d" --config-subdir . --work-dir "$(pwd)/gen/work" &

# 從 repo root build 一個本機 image（go.mod 需 threshold-exporter 模組）
docker build -t tenant-api -f "$REPO/components/tenant-api/Dockerfile" "$REPO"

# API 只在 Unix socket 上、經同一個 oauth2-proxy(ADR-027 §2);兩者讀同一份結構化設定檔。
# 這裡沿用 try-local 的檔:oauth2-proxy 對所有請求免登入,由 tenant-api 的開發旁路注入身分。
mkdir -p sock oauth2-proxy
cp "$REPO/try-local/oauth2-proxy/oauth2-proxy.yaml" oauth2-proxy/

# 以你的 uid 跑(與產生器同 uid);TA_STATE_*：tenant-api 自己的紀錄（群組、views…）存在 conf.d 之外，必填
# 8083 只有 /health、/ready、/metrics
docker run --rm -d --name tenant-api -p 127.0.0.1:8083:8080 --user "$(id -u):$(id -g)" \
  -e TA_RBAC_PATH=/conf.d/_rbac.yaml \
  -e TA_STATE_STORE=dir -e TA_STATE_DIR=/state \
  -e TA_GENERATOR_SOCKET=/run/da-generator/generator.sock \
  -e TA_HUMAN_SOCKET=/run/ta-human/human.sock \
  -e TA_OAUTH2_PROXY_CONFIG=/etc/oauth2-proxy/oauth2-proxy.yaml \
  -e TA_DEV_BYPASS_AUTH=true \
  -v "$(pwd)/conf.d:/conf.d" -v "$(pwd)/gen:/run/da-generator" -v "$(pwd)/state:/state" \
  -v "$(pwd)/sock:/run/ta-human" -v "$(pwd)/oauth2-proxy:/etc/oauth2-proxy:ro" tenant-api

# API 入口:oauth2-proxy(同 uid 才連得上 0660 的 socket)
docker run --rm -d --name tenant-api-proxy -p 127.0.0.1:8080:8080 --user "$(id -u):$(id -g)" \
  -v "$(pwd)/sock:/run/ta-human" -v "$(pwd)/oauth2-proxy:/etc/oauth2-proxy:ro" \
  quay.io/oauth2-proxy/oauth2-proxy:v7.15.6 \
  --alpha-config=/etc/oauth2-proxy/oauth2-proxy.yaml \
  --trusted-ip=0.0.0.0/0 --trusted-ip=::/0 --email-domain='*' \
  --cookie-secret=local-dev-not-a-secret-32-bytes! --cookie-secure=false
```

### 3) 我是誰、能管哪些租戶(零 DB 直接回)

```sh
curl -s localhost:8080/api/v1/me
```

你會看到(HTTP 200):

```json
{"email":"dev@local","user":"dev","groups":["demo-admins"],
 "accessible_tenants":["*"],"permissions":{"demo-admins":["admin","read","write"]}}
```

全程**沒有任何資料庫**——身分來自 RBAC 規則、租戶 config 是檔案。清理:`docker rm -f tenant-api tenant-api-proxy`,再停掉背景的產生器服務(`kill %1`)。

> ⚠️ **僅限 localhost dev**:這裡的 oauth2-proxy 對所有請求免登入、身分由開發旁路注入([ADR-022](../../docs/adr/022-dev-auth-bypass-four-layer-containment.md);在 Kubernetes 內啟動會 panic)。production 由 oauth2-proxy 登入後經 Unix socket 注入身分 header;tenant-api 的 TCP port 只有 `/health`、`/ready`、`/metrics`,帶身分 header 的請求回 400。

---

## 路徑 B — API 整合 / 自動化:直接呼叫

接上面的服務。所有 write 端點走相同的 RBAC + 寫回流程,適合 CI / 腳本。

```sh
H=''   # 身分由開發旁路注入;production 經 oauth2-proxy 登入

# 讀:列出可見租戶 / 取得單一租戶的 effective config
curl -s $H localhost:8080/api/v1/tenants
curl -s $H localhost:8080/api/v1/tenants/db-a/effective

# 寫前先 dry-run 驗證（不寫入；格式錯誤回 400）
curl -s $H -X POST localhost:8080/api/v1/tenants/db-a/validate \
  --data-binary $'tenants:\n  db-a:\n    mysql_connection_usage_percent: "90"\n'

# 寫入（direct 模式 → 直接 git commit 進 conf.d）
curl -s $H -X PUT localhost:8080/api/v1/tenants/db-a \
  --data-binary $'tenants:\n  db-a:\n    mysql_connection_usage_percent: "90"\n'
```

> 其他整合面:批次 `POST /api/v1/tenants/batch`(`?async=true` 走 task 池,`/api/v1/tasks/{id}` polling)、即時事件 `GET /api/v1/events`(SSE)、聯邦 token 簽發 `POST /api/v1/federation/tokens`。完整端點與權限見 **[README §API 參考](README.md#api-參考)**。

---

## GitOps 寫回 — 看完整閉環

tenant-api 的旗艦能力是**寫回成 git**:portal 改一個閾值 → tenant-api commit 進 conf.d(`direct` 模式)或開 PR/MR(`pr-github` / `pr-gitlab`)。完整閉環需要完整 seed(RBAC + `_defaults.yaml` schema + git repo),已在 **try-local** 一鍵備妥:在那裡於 portal 按 Save,會在 host 掛載的 seed repo 看到**真實 git commit**,並觸發 exporter 熱重載與告警。

> Production 的 PR/MR 寫回(`TA_WRITE_MODE=pr-github` / `pr-gitlab` + token + repo)把每筆變更開成**可審查 PR/MR**。

## 下一步

- ← **先玩整套**:[`try-local/`](../../try-local/)(portal 改 config → tenant-api commit → exporter 熱重載 → 告警紅燈)
- 📖 **完整 API / 設定參考**:[`README.md`](README.md)
- → **上 production**:[平台工程師指南 §部署 tenant-api](../../docs/getting-started/for-platform-engineers.md)(oauth2-proxy、PR/MR 寫回、HA)
- 🧑‍💼 **你是想自助改告警的租戶?** → [租戶指南](../../docs/getting-started/for-tenants.md)(用 portal,免寫 PromQL)
