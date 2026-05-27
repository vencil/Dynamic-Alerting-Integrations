# tenant-api — Self-service tenant config with Git write-back, no database

> **file-based 的租戶自助配置 API：租戶改自己的 config → 寫回成真實 git commit / 可審查 PR，零 DB、零外部依賴。**
> *File-based self-service tenant config API — changes write back as Git commits/PRs. No database.*

|  |  |
|---|---|
| **What / 是什麼** | 租戶配置管理 API，GitOps 原生（commit-on-write / PR write-back）、RBAC-scoped。*Tenant config API, GitOps-native, RBAC-scoped.* |
| **Why / 為什麼** | 零 DB——一行 `docker run` 就跑；身分與權限走 RBAC，寫回走 git。*Zero DB; identity/permission via RBAC, writes via Git.* |
| **Who / 給誰** | 平台維運 / 想自助的租戶 |
| **Try（≤5 min）** | 見下方 / *steps below* |
| **→ You'll see** | `curl /api/v1/me` 從一個**零 DB、RBAC-scoped** 的 API 回你的身分與權限（HTTP 200）。*Your identity + RBAC permissions from a zero-DB API.* |

> 🎯 **主要服務對象**：Platform Engineer（部署 / 整合 oauth2-proxy / PR 寫回，見 [Platform Engineer 角色指南](../../docs/getting-started/for-platform-engineers.md)）；想自助的租戶也透過它改 config。

**Prerequisite**：Docker 20.10+、`curl`。

## Try it

### 1) 準備一個 demo config 目錄（含 RBAC 規則 + 一個租戶）

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

### 2) 跑 tenant-api（**零 DB**；用 `TA_RBAC_PATH` 啟用 RBAC——否則為 open-read 模式）

```sh
docker run --rm -d --name tenant-api -p 8080:8080 \
  -e TA_RBAC_PATH=/conf.d/_rbac.yaml \
  -v "$(pwd)/conf.d:/conf.d" ghcr.io/vencil/tenant-api:v2.8.0
```

### 3) 我是誰、能管哪些租戶（零 DB 直接回）

```sh
curl -s -H 'X-Forwarded-Email: dev@local' -H 'X-Forwarded-Groups: demo-admins' \
  localhost:8080/api/v1/me
```

**你會看到**（HTTP 200，實測）：

```json
{"email":"dev@local","user":"dev","groups":["demo-admins"],
 "accessible_tenants":["*"],"permissions":{"demo-admins":["admin","read","write"]}}
```

整個過程**沒有任何資料庫**——身分來自 RBAC 規則、租戶 config 是檔案。清理：`docker rm -f tenant-api`。

> **dev 注意**：production 由 oauth2-proxy 在前面注入 `X-Forwarded-*` 身分 header，tenant-api **信任**它們。此處手動帶 header 只適用 **localhost dev**——切勿把這個無 proxy 的接法搬上 production。

## GitOps write-back — 看完整閉環

tenant-api 的旗艦能力是**寫回成 git**：portal 改一個閾值 → tenant-api commit 進 conf.d（`direct` 模式）或開 PR/MR（`pr-github` / `pr-gitlab`）。這條閉環需要完整 seed（RBAC + `_defaults.yaml` schema + git repo），已在 **try-local** 一鍵備妥：在那裡於 portal 按 Save，會在 host 掛載的 seed repo 看到**真實 git commit**，並觸發 exporter 熱重載與告警。

> Production 的 PR/MR write-back（`TA_WRITE_MODE=pr-github` / `pr-gitlab` + token + repo）把每筆變更開成**可審查 PR/MR**——其真實 forge 整合由 CI E2E 驗證（見 [#616](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/616)）。

## Next
- ← **先玩整套**：[`try-local/`](../../try-local/)（portal 改 config → tenant-api commit → exporter 熱重載 → 告警紅燈，完整 GitOps 閉環）
- 📖 **深入配置 / API 參考**：[`README.md`](README.md)（Does/Doesn't、完整 endpoint、寫回模式）
- → **上 production**：[`helm/tenant-api/`](../../helm/tenant-api/)（oauth2-proxy、PR/MR write-back、federation）
