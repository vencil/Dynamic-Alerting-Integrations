---
title: "GitLab CE SSO Integration Guide"
tags: [integration, gitlab, sso, oauth2-proxy, oidc]
audience: [platform-engineer, sre]
version: v2.9.0
lang: en
---

# GitLab CE SSO Integration Guide

> **Language / 語言：** [中文](./gitlab-ce-sso.md) | **English (Current)**

> Use **self-hosted GitLab Community Edition** as the platform SSO — to sign in to the portal and tenant-api (both in one pod, behind one oauth2-proxy — [ADR-027 §5](../adr/027-tenant-api-caller-identity.en.md)). No GitLab Enterprise (EE) features are required. How it works: oauth2-proxy's `gitlab` provider authenticates the user, resolves group membership from `/oauth/userinfo`, and injects `X-Forwarded-Groups`; tenant-api authorizes via [`_rbac.yaml`](../governance-security.en.md).

## Prerequisites

- self-hosted GitLab CE (Free self-managed) is enough — **no EE**.
- The platform ingress uses **HTTPS** (required by the `Secure` cookie).
- An account that can create an OAuth application in GitLab (a user/group-owned application needs **no admin**; instance-wide needs admin).

## Step 1: Register an OAuth application in GitLab

In GitLab **User Settings → Applications** (or Group / Admin, per scope), create:

- **Redirect URI**: `https://<your-portal-host>/oauth2/callback` (the portal and the API share one host behind one oauth2-proxy, so this one is all you need).
- **Scopes**: check `openid`, `profile`, `email`. **Groups need no extra scope** — under `openid` the full (inherited) group set is available from `/oauth/userinfo`. `read_api` is only needed if you use `--gitlab-group` for **project** filtering.
- Record the **Application ID** and **Secret**.

## Step 2: Configure Helm values

Put GitLab's client credentials into the existing `oauth2-proxy-secrets` (keys `OAUTH2_PROXY_CLIENT_SECRET` / `OAUTH2_PROXY_COOKIE_SECRET`). The chart reads the existing secret; no new channel is added.

The tenant-api chart runs oauth2-proxy with a structured config file ([ADR-027](../adr/027-tenant-api-caller-identity.en.md) §10), so **the Application ID goes in the `oauth2Proxy.clientID` value**; the secret's `OAUTH2_PROXY_CLIENT_ID` is not read. GitLab's extra claims (an organization, say) are declared with `oauth2Proxy.claimHeaders` (which header carries each), and tenant-api learns them from the same file (see the [tenant-api README](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/tenant-api/README.md), "身分 header 由 oauth2-proxy 設定檔決定").

Configure the `oauth2Proxy` block in the tenant-api chart (the deployment's only oauth2-proxy; the da-portal chart is a static, no-login deployment now):

```yaml
oauth2Proxy:
  provider: gitlab
  clientID: "<Application ID>"
  oidcIssuerUrl: "https://gitlab.acme.internal"   # your bare GitLab instance URL
  scope: "openid profile email"
  redirectUrl: "https://portal.acme.internal/oauth2/callback"
  cookieSecure: true                               # requires HTTPS
  # Optional: restrict login to specific GitLab groups (full paths, repeatable)
  gitlabGroups: ["acme/sre", "acme/dba"]
  # Optional: revocation-lag guard (see below)
  cookieExpire: "4h"
  cookieRefresh: "1h"
```

Leaving `oidcIssuerUrl` empty keeps the existing `github` provider behavior (these flags are not rendered).

## Step 3: Map GitLab groups into `_rbac.yaml`

GitLab groups appear as **full paths** (e.g. `acme/sre`, `acme/platform/oncall`), delivered via `X-Forwarded-Groups`. tenant-api does **exact-string** matching on group names, so the `name` in `_rbac.yaml` must be GitLab's **actual full path**:

```yaml
groups:
  - name: acme/platform-admins      # ← GitLab group full path, not a flat alias
    tenants: ["*"]
    permissions: [read, write, admin]
  - name: acme/dba
    tenants: ["db-a-*", "db-b-*"]
    permissions: [read, write]
```

Common mistake: using a group **display name** or slug instead of the full path → no match → the user maps to zero tenants and sees a blank screen (Tenant Manager shows a soft notice banner). **Inherited** subgroup memberships are covered by oauth2-proxy's userinfo call (the `gitlab` provider already uses it); no extra config needed.

## Step 4: Pre-cutover validation (required)

Before deploying, test against the customer's GitLab to close every unknown:

```bash
# 1) Confirm GitLab exposes OIDC discovery (issuer / jwks / userinfo endpoint)
curl -s https://gitlab.acme.internal/.well-known/openid-configuration | jq '{issuer, userinfo_endpoint, jwks_uri}'

# 2) With a real user's token, confirm /oauth/userinfo returns full-path groups incl. nested subgroups
curl -s -H "Authorization: Bearer <access_token>" \
  https://gitlab.acme.internal/oauth/userinfo | jq '.groups'
```

`.groups` should list all of the user's (including inherited) group full paths. If a subgroup is missing, check GitLab group membership and the scope.

## Session lifetime and revocation lag

Groups are resolved **at login** and cached in the session cookie. The following describes oauth2-proxy v7.15.6, which the chart pins:

- **Without `cookieRefresh`** (the chart default): there is no refresh. The groups are frozen as of login, and the cookie expires at `cookieExpire`, after which the user signs in again.
- **With `cookieRefresh`**: every interval, the session redeems its refresh token with GitLab. A successful refresh resets the session's creation time and re-issues the cookie, so the cookie's lifetime moves forward with every successful refresh. When the response carries a new ID token, the groups are replaced from its `groups` claim; **when it does not, the groups stay as they were** (the gitlab provider's `RefreshSession` uses the OIDC refresh and does not re-read userinfo). When the refresh token is revoked (GitLab answers `invalid_grant`), the session is cleared.

This is inherent to oauth2-proxy sessions, and the platform lists it as an **accepted limitation** ([ADR-027](../adr/027-tenant-api-caller-identity.en.md) §8); it is not instant revocation:

- Without `cookieRefresh`: access from a removed group lasts at most until `cookieExpire`.
- With `cookieRefresh`, and GitLab's refresh responses carry a new ID token: at most one `cookieRefresh`.
- With `cookieRefresh`, but the responses carry no new ID token: **there is no time bound**. The old groups carry over on every successful refresh until a refresh fails.

So before setting `cookieRefresh`, remove a test account from a group in staging and confirm the access is gone one `cookieRefresh` later. If it is not, leave `cookieRefresh` unset so that `cookieExpire` is a hard bound.

## Identity trust and the production prerequisite

tenant-api trusts only the identity oauth2-proxy sends: the API is only on a Unix socket inside the pod, behind oauth2-proxy, and oauth2-proxy strips any same-named header the client sends; TCP 8080 serves only `/health`, `/ready` and `/metrics`, and any request carrying an identity header gets 400 ([ADR-027](../adr/027-tenant-api-caller-identity.en.md) §2, §3).

**Production requires a tenant-api release that includes ADR-027** (the version is filled in when that release ships). Older tenant-api releases trust the `X-Forwarded-Groups` a request carries: anyone who can reach it without going through oauth2-proxy can claim any group. The chart's NetworkPolicy lets only Prometheus (older charts: also threshold-govern) reach 8080, but **only where the CNI enforces NetworkPolicy**: where it does not, 8080 is open to the whole cluster, and with an older tenant-api any pod in the cluster can claim any group. Before upgrading, confirm that your CNI enforces it, as the chart's `NOTES.txt` describes.

To check which one your cluster runs, go through `kubectl port-forward` (not subject to NetworkPolicy) and call it locally. The chart pins a single replica updated with `Recreate` (ADR-023), so once the rollout completes there is one pod, and that is the one you reach.

```bash
# Let the system pick the local port (:8080) and probe only once kubectl prints the port it bound; if nothing
# is bound within 10 seconds, skip the probe and print kubectl's output, so you never read another program's answer
LOG=$(mktemp); PORT=
kubectl -n <namespace> port-forward --address 127.0.0.1 svc/<tenant-api service> :8080 >"$LOG" 2>&1 & PF=$!
for i in $(seq 10); do
  PORT=$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9]*\) .*/\1/p' "$LOG"); [ -n "$PORT" ] && break; sleep 1
done
# /health with an identity header: 400 = includes ADR-027; 200 = older release; 000 = no connection
[ -n "$PORT" ] && curl -s --max-time 5 -o /dev/null -w '%{http_code}\n' \
  -H 'X-Forwarded-Email: probe@example.com' "http://127.0.0.1:$PORT/health" || cat "$LOG"
kill $PF 2>/dev/null
```

## Troubleshooting

- **Login loop / cookie not sent**: `cookieSecure: true` requires HTTPS. Either add TLS to the ingress or temporarily set `false` (testing only).
- **Redirect URI mismatch**: the GitLab application's Redirect URI must match `redirectUrl` verbatim (including scheme and the `/oauth2/callback` suffix).
- **Blank screen after login**: usually the `_rbac.yaml` group names are not GitLab full paths (see Step 3).
- **4xx / `431 Request Header Fields Too Large` after login**: for users in **many GitLab groups** (including inherited ones), the session cookie (which encodes the groups and is sent by the client on every request) and the oauth2-proxy-injected `X-Forwarded-Groups` header can grow long enough to exceed the default header/buffer limit of the **ingress controller** or the **portal container's nginx**, and the request is dropped. Raise that layer's buffers (nginx-ingress: `nginx.ingress.kubernetes.io/proxy-buffer-size` + `large_client_header_buffers`), or narrow the login surface with `gitlabGroups`. (The tenant-api side is a Go app with a generous `MaxHeaderBytes`, so it is less likely to hit this.)
- **Allow only specific groups**: use `gitlabGroups` (blocks login at the proxy); fine-grained authorization is still decided by `_rbac.yaml`.
