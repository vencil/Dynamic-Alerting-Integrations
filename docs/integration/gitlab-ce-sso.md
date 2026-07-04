---
title: "GitLab CE SSO 整合指南"
tags: [integration, gitlab, sso, oauth2-proxy, oidc]
audience: [platform-engineer, sre]
version: v2.9.0
lang: zh
---

# GitLab CE SSO 整合指南

> **Language / 語言：** **中文 (Current)** | [English](./gitlab-ce-sso.en.md)

> 用 **self-hosted GitLab Community Edition** 當平台 SSO——登入 da-portal 與 tenant-api。全程免 GitLab Enterprise（EE）功能。原理：oauth2-proxy 的 `gitlab` provider 認證使用者、從 `/oauth/userinfo` 解出群組並注入 `X-Forwarded-Groups`，tenant-api 依 [`_rbac.yaml`](../governance-security.md) 授權。

## 適用前提

- self-hosted GitLab CE（免費版 / Free self-managed）即可，**無需 EE**。
- 平台 ingress 走 **HTTPS**（cookie `Secure` 需要）。
- 你有能在 GitLab 建立 OAuth application 的帳號（user/group-owned application **免管理員**；instance-wide 才需管理員）。

## 步驟一：在 GitLab 註冊 OAuth application

在 GitLab **User Settings → Applications**（或 Group / Admin，依範圍）建立：

- **Redirect URI**：`https://<你的-portal-host>/oauth2/callback`（tenant-api 另一個 host 則各自加一條，或用同一 oauth2-proxy 前端）。
- **Scopes**：勾 `openid`、`profile`、`email`。**群組不需要額外 scope**——`openid` 下即可從 `/oauth/userinfo` 取得完整（含繼承）群組。`read_api` 只有在要用 `--gitlab-group` 做**專案**過濾時才需要。
- 建好後記下 **Application ID** 與 **Secret**。

## 步驟二：設定 Helm values

把 GitLab 的 client 憑證放進既有的 `oauth2-proxy-secrets`（key `OAUTH2_PROXY_CLIENT_ID` / `OAUTH2_PROXY_CLIENT_SECRET` / `OAUTH2_PROXY_COOKIE_SECRET`）。charts 讀既有 secret，不新增管道。

tenant-api 與 da-portal 兩個 chart 的 `oauth2Proxy` 區塊設定一致（單一 SSO）：

```yaml
oauth2Proxy:
  provider: gitlab
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

群組在**登入時**解析並存入 session cookie。**設了 `cookieRefresh` 時**，session 每過該間隔會**重新向 GitLab 驗證帳號並覆寫群組**（gitlab provider 的 `RefreshSession` 行為）——故群組/帳號撤權延遲**被 `cookieRefresh` 綁住（非 `cookieExpire`）**：設短 `cookieRefresh`（例如 1h）即收緊，被移出群組或停用的使用者於下次 refresh 失效。**未設 `cookieRefresh`** 則群組凍結在登入當下、直到 `cookieExpire` 到期。無論如何這都是**緩解、非即時撤權**——後端仍盲信 `X-Forwarded-Groups`，真正的即時撤權要等身份硬化（後端不再信未驗 header，見平台安全路線）落地，故 **production cutover 待其完成**。

## 疑難排解

- **登入迴圈 / cookie 不送**：`cookieSecure: true` 需 HTTPS。ingress 未上 TLS 時要嘛補 TLS、要嘛暫設 `false`（僅測試）。
- **Redirect URI mismatch**：GitLab application 的 Redirect URI 必須逐字比對 `redirectUrl`（含 scheme 與 `/oauth2/callback` 尾段）。
- **登入後全空畫面**：多半是 `_rbac.yaml` 群組名非 GitLab 全路徑（見步驟三）。
- **登入後 4xx／`431 Request Header Fields Too Large`**：使用者隸屬**大量 GitLab 群組**（含繼承）時，session cookie（編碼 groups，每個請求由 client 帶回）與 oauth2-proxy 注入的 `X-Forwarded-Groups` 表頭會很長，可能超過 **ingress controller** 或 **da-portal 內 nginx** 的預設 header/buffer 上限而被丟棄。調高該層 buffer（nginx-ingress：`nginx.ingress.kubernetes.io/proxy-buffer-size` + `large_client_header_buffers`），或用 `gitlabGroups` 縮登入群組面。（tenant-api 側是 Go app、`MaxHeaderBytes` 較寬鬆，較不會中。）
- **只想放行特定群組**：用 `gitlabGroups`（proxy 層擋登入）；細粒度授權仍由 `_rbac.yaml` 決定。
