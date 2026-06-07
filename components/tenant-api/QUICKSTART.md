# tenant-api — 5 分鐘跑起來

> 零資料庫的租戶配置 API:身分走 RBAC、寫入落成 git commit / 可審查 PR。一行 `docker run` 就跑。
>
> *Zero-DB tenant config API — identity via RBAC, writes via Git. One `docker run`.*

| | |
|---|---|
| **是什麼** | GitOps 原生、RBAC-scoped 的租戶配置 API(commit-on-write / PR 寫回) |
| **給誰** | **平台工程師 / 維運**(部署)與 **API 整合 / 自動化**(直接呼叫)。<br>**人類租戶**改告警請用 **Tenant Manager portal**,不需碰這個 API。 |
| **前置** | Docker 20.10+、`curl` |
| **你會看到** | `curl /api/v1/me` 從零 DB 回你的身分與 RBAC 權限(HTTP 200) |

> 版本:範例用本機 build 的 image。要拉 published image 時版本見 [Releases / CHANGELOG](../../CHANGELOG.md)。

---

## 路徑 A — 維運:把服務跑起來、確認身分模型

### 1) 準備一個 demo config 目錄(RBAC 規則 + 一個租戶)

```sh
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
    mysql_connections: "70"
EOF
```

### 2) 跑 tenant-api(零 DB;`TA_RBAC_PATH` 啟用 RBAC,否則為 open-read)

```sh
# 從 repo root build 一個本機 image（go.mod 需 threshold-exporter 模組）
docker build -t tenant-api -f components/tenant-api/Dockerfile .

docker run --rm -d --name tenant-api -p 8080:8080 \
  -e TA_RBAC_PATH=/conf.d/_rbac.yaml \
  -v "$(pwd)/conf.d:/conf.d" tenant-api
```

### 3) 我是誰、能管哪些租戶(零 DB 直接回)

```sh
curl -s -H 'X-Forwarded-Email: dev@local' -H 'X-Forwarded-Groups: demo-admins' \
  localhost:8080/api/v1/me
```

你會看到(HTTP 200):

```json
{"email":"dev@local","user":"dev","groups":["demo-admins"],
 "accessible_tenants":["*"],"permissions":{"demo-admins":["admin","read","write"]}}
```

全程**沒有任何資料庫**——身分來自 RBAC 規則、租戶 config 是檔案。清理:`docker rm -f tenant-api`。

> ⚠️ **僅限 localhost dev**:production 由 oauth2-proxy 在前面注入 `X-Forwarded-*` 身分 header,本服務**信任**它們。手動帶 header 只適用本機;切勿把這個無 proxy 的接法搬上 production。

---

## 路徑 B — API 整合 / 自動化:直接呼叫

接上面的服務。所有 write 端點走相同的 RBAC + 寫回流程,適合 CI / 腳本。

```sh
H='-H X-Forwarded-Email:dev@local -H X-Forwarded-Groups:demo-admins'

# 讀:列出可見租戶 / 取得單一租戶的 effective config
curl -s $H localhost:8080/api/v1/tenants
curl -s $H localhost:8080/api/v1/tenants/db-a/effective

# 寫前先 dry-run 驗證（不寫入；格式錯誤回 400）
curl -s $H -X POST localhost:8080/api/v1/tenants/db-a/validate \
  --data-binary $'tenants:\n  db-a:\n    mysql_connections: "90"\n'

# 寫入（direct 模式 → 直接 git commit 進 conf.d）
curl -s $H -X PUT localhost:8080/api/v1/tenants/db-a \
  --data-binary $'tenants:\n  db-a:\n    mysql_connections: "90"\n'
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
