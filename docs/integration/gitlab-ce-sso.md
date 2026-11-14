---
title: "GitLab CE SSO 整合指南"
tags: [integration, gitlab, sso, oauth2-proxy, oidc]
audience: [platform-engineer, sre]
version: v2.9.0
lang: zh
---

# GitLab CE SSO 整合指南

> **Language / 語言：** **中文 (Current)** | [English](./gitlab-ce-sso.en.md)

> 用 **self-hosted GitLab Community Edition** 當平台 SSO——登入 portal 與 tenant-api（兩者在同一個 pod、共用一個 oauth2-proxy，[ADR-027 §5](../adr/027-tenant-api-caller-identity.md)）。全程免 GitLab Enterprise（EE）功能。原理：oauth2-proxy 的 `gitlab` provider 認證使用者、從 `/oauth/userinfo` 解出群組並注入 `X-Forwarded-Groups`，tenant-api 依 [`_rbac.yaml`](../governance-security.md) 授權。

## 適用前提

- self-hosted GitLab CE（免費版 / Free self-managed）即可，**無需 EE**。
- 平台 ingress 走 **HTTPS**（cookie `Secure` 需要）。
- 你有能在 GitLab 建立 OAuth application 的帳號（user/group-owned application **免管理員**；instance-wide 才需管理員）。

## 步驟一：在 GitLab 註冊 OAuth application

在 GitLab **User Settings → Applications**（或 Group / Admin，依範圍）建立：

- **Redirect URI**：`https://<你的-portal-host>/oauth2/callback`（portal 與 API 在同一個 host、同一個 oauth2-proxy 後面，只需要這一條）。
- **Scopes**：勾 `openid`、`profile`、`email`。**群組不需要額外 scope**——`openid` 下即可從 `/oauth/userinfo` 取得完整（含繼承）群組。`read_api` 只有在要用 `--gitlab-group` 做**專案**過濾時才需要。
- 建好後記下 **Application ID** 與 **Secret**。

## 步驟二：設定 Helm values

把 GitLab 的 client 憑證放進既有的 `oauth2-proxy-secrets`（key `OAUTH2_PROXY_CLIENT_SECRET` / `OAUTH2_PROXY_COOKIE_SECRET`）。chart 讀既有 secret，不新增管道。

tenant-api chart 的 oauth2-proxy 以結構化設定檔執行（[ADR-027](../adr/027-tenant-api-caller-identity.md) §10），**Application ID 寫在 values 的 `oauth2Proxy.clientID`**，不讀 secret 的 `OAUTH2_PROXY_CLIENT_ID`。GitLab 的額外 claim（例如組織）以 `oauth2Proxy.claimHeaders` 宣告注入哪個 header，tenant-api 從同一份設定檔得知（見 [tenant-api README](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/tenant-api/README.md)「身分 header 由 oauth2-proxy 設定檔決定」）。

在 tenant-api chart 設定 `oauth2Proxy` 區塊（整個部署只有這一個 oauth2-proxy；da-portal chart 只剩不經登入的純靜態部署）：

```yaml
oauth2Proxy:
  provider: gitlab
  clientID: "<Application ID>"
  oidcIssuerUrl: "https://gitlab.acme.internal"   # 你的 GitLab 裸 instance URL
  scope: "openid profile email"
  redirectUrl: "https://portal.acme.internal/oauth2/callback"
  cookieSecure: true                               # 需 HTTPS
  # 選用：限制只有特定 GitLab 群組可登入（全路徑，可多條）
  gitlabGroups: ["acme/sre", "acme/dba"]
  # 選用：撤權延遲護欄（見下）
  cookieExpire: "4h"
  cookieRefresh: "1h"
```

留空 `oidcIssuerUrl` 即維持既有 `github` provider 行為（不 render 這些 flag）。

## 步驟三：把 GitLab 群組對映進 `_rbac.yaml`

GitLab 群組以**全路徑**出現（例如 `acme/sre`、`acme/platform/oncall`），透過 `X-Forwarded-Groups` 傳入。tenant-api 對群組名做 **exact-string 比對**，所以 `_rbac.yaml` 的 `name` 必須填 GitLab 的**實際全路徑**：

```yaml
groups:
  - name: acme/platform-admins      # ← GitLab 群組全路徑，非扁平代稱
    tenants: ["*"]
    permissions: [read, write, admin]
  - name: acme/dba
    tenants: ["db-a-*", "db-b-*"]
    permissions: [read, write]
```

常見錯誤：填了群組**顯示名**或 slug 而非全路徑 → 比對不到 → 該使用者對應到零租戶、畫面全空（Tenant Manager 會顯示 soft 提示 banner）。子群組的**繼承**成員身份由 oauth2-proxy 的 userinfo 呼叫涵蓋（`gitlab` provider 已用），無需額外設定。

## 步驟四：上線前驗證（必跑）

部署前對客戶 GitLab 實測，收掉所有不確定：

```bash
# 1) 確認 GitLab 有 OIDC discovery（issuer / jwks / userinfo endpoint）
curl -s https://gitlab.acme.internal/.well-known/openid-configuration | jq '{issuer, userinfo_endpoint, jwks_uri}'

# 2) 用一個真使用者的 token 確認 /oauth/userinfo 回傳「含巢狀子群組」的全路徑 groups
curl -s -H "Authorization: Bearer <access_token>" \
  https://gitlab.acme.internal/oauth/userinfo | jq '.groups'
```

`.groups` 應列出該使用者所有（含繼承）群組的全路徑。若缺子群組，檢查 GitLab 群組成員身份與 scope。

## Session 生命週期與撤權延遲

群組在**登入時**解析並存入 session cookie。以下依 chart 釘的 oauth2-proxy v7.15.6：

- **未設 `cookieRefresh`**（chart 預設）：不做 refresh，群組凍結在登入當下，cookie 在 `cookieExpire` 到期，到期即須重新登入。
- **設了 `cookieRefresh`**：session 每過該間隔用 refresh token 向 GitLab 換新 token。refresh 成功會重設 session 的建立時間並重新簽發 cookie，所以 cookie 的有效期隨每次成功的 refresh 往後延。回應帶新的 ID token 時，群組改用它的 `groups` claim；**沒有時群組維持原值**（gitlab provider 的 `RefreshSession` 走 OIDC 的 refresh，不重讀 userinfo）。refresh token 被撤銷（GitLab 回 `invalid_grant`）時 session 會被清除。

這是 oauth2-proxy session 的本質，平台把它列為**已接受的限制**（[ADR-027](../adr/027-tenant-api-caller-identity.md) §8），不是即時撤權：

- 未設 `cookieRefresh`：被移出群組的權限最多保留到 `cookieExpire`。
- 設了 `cookieRefresh` 且 GitLab 的 refresh 回應帶新的 ID token：最多保留一個 `cookieRefresh`。
- 設了 `cookieRefresh` 但回應不帶新的 ID token：**沒有時間上限**，舊群組會隨每次成功的 refresh 一直延續，直到 refresh 失敗為止。

所以設 `cookieRefresh` 之前，先在 staging 把測試帳號移出一個群組，等過一個 `cookieRefresh` 確認權限真的消失。權限沒有消失，就不要設 `cookieRefresh`，讓 `cookieExpire` 成為硬上限。

## 身分信任與 production 前提

tenant-api 只採信 oauth2-proxy 送來的身分：API 只在 pod 內的 Unix socket 上，前面是 oauth2-proxy，client 自己帶的同名 header 會被 oauth2-proxy 清掉；TCP 8080 只有 `/health`、`/ready`、`/metrics`，帶身分 header 一律回 400（[ADR-027](../adr/027-tenant-api-caller-identity.md) §2、§3）。

**production 需部署含 ADR-027 的 tenant-api**（版號於該版發布時補上）。舊版 tenant-api 直接採信請求帶的 `X-Forwarded-Groups`：能繞過 oauth2-proxy 連到它的人，可以自稱任何群組。chart 的 NetworkPolicy 只讓 Prometheus（舊版 chart 另含 threshold-govern）連 8080，但**只有 CNI 執行 NetworkPolicy 時才有效**：CNI 不執行時 8080 對整個叢集開放，在舊版 tenant-api 下等於叢集裡任何 pod 都能自稱任何群組。升級前先依 chart 的 `NOTES.txt` 確認 CNI 會執行它。

確認叢集裡跑的是哪一種：經 `kubectl port-forward`（不受 NetworkPolicy 約束）在本機打。chart 固定單一副本、以 `Recreate` 更新（ADR-023），rollout 完成後只有一個 pod，打到的就是它。

```bash
# 本機埠交給系統指派（:8080），等 kubectl 印出綁定的埠才打；10 秒內沒綁上就不打、改印 kubectl 的輸出，
# 以免讀到本機別的程式的回應
LOG=$(mktemp); PORT=
kubectl -n <namespace> port-forward --address 127.0.0.1 svc/<tenant-api service> :8080 >"$LOG" 2>&1 & PF=$!
for i in $(seq 10); do
  PORT=$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9]*\) .*/\1/p' "$LOG"); [ -n "$PORT" ] && break; sleep 1
done
# 帶身分 header 打 /health：400 = 含 ADR-027；200 = 舊版；000 = 沒連上
[ -n "$PORT" ] && curl -s --max-time 5 -o /dev/null -w '%{http_code}\n' \
  -H 'X-Forwarded-Email: probe@example.com' "http://127.0.0.1:$PORT/health" || cat "$LOG"
kill $PF 2>/dev/null
```

## 疑難排解

- **登入迴圈 / cookie 不送**：`cookieSecure: true` 需 HTTPS。ingress 未上 TLS 時要嘛補 TLS、要嘛暫設 `false`（僅測試）。
- **Redirect URI mismatch**：GitLab application 的 Redirect URI 必須逐字比對 `redirectUrl`（含 scheme 與 `/oauth2/callback` 尾段）。
- **登入後全空畫面**：多半是 `_rbac.yaml` 群組名非 GitLab 全路徑（見步驟三）。
- **登入後 4xx／`431 Request Header Fields Too Large`**：使用者隸屬**大量 GitLab 群組**（含繼承）時，session cookie（編碼 groups，每個請求由 client 帶回）與 oauth2-proxy 注入的 `X-Forwarded-Groups` 表頭會很長，可能超過 **ingress controller** 或 **portal 容器 nginx** 的預設 header/buffer 上限而被丟棄。調高該層 buffer（nginx-ingress：`nginx.ingress.kubernetes.io/proxy-buffer-size` + `large_client_header_buffers`），或用 `gitlabGroups` 縮登入群組面。（tenant-api 側是 Go app、`MaxHeaderBytes` 較寬鬆，較不會中。）
- **只想放行特定群組**：用 `gitlabGroups`（proxy 層擋登入）；細粒度授權仍由 `_rbac.yaml` 決定。
