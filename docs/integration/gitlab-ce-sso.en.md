---
title: "GitLab CE SSO Integration Guide"
tags: [integration, gitlab, sso, oauth2-proxy, oidc]
audience: [platform-engineer, sre]
version: v2.9.0
lang: en
---

# GitLab CE SSO Integration Guide

> **Language / 語言：** [中文](./gitlab-ce-sso.md) | **English (Current)**

> Use **self-hosted GitLab Community Edition** as the platform SSO — to sign in to da-portal and tenant-api. No GitLab Enterprise (EE) features are required. How it works: oauth2-proxy's `gitlab` provider authenticates the user, resolves group membership from `/oauth/userinfo`, and injects `X-Forwarded-Groups`; tenant-api authorizes via [`_rbac.yaml`](../governance-security.en.md).

## Prerequisites

- self-hosted GitLab CE (Free self-managed) is enough — **no EE**.
- The platform ingress uses **HTTPS** (required by the `Secure` cookie).
- An account that can create an OAuth application in GitLab (a user/group-owned application needs **no admin**; instance-wide needs admin).

## Step 1: Register an OAuth application in GitLab

In GitLab **User Settings → Applications** (or Group / Admin, per scope), create:

- **Redirect URI**: `https://<your-portal-host>/oauth2/callback` (add a separate one if tenant-api is on another host, or front both with one oauth2-proxy).
- **Scopes**: check `openid`, `profile`, `email`. **Groups need no extra scope** — under `openid` the full (inherited) group set is available from `/oauth/userinfo`. `read_api` is only needed if you use `--gitlab-group` for **project** filtering.
- Record the **Application ID** and **Secret**.

## Step 2: Configure Helm values

Put GitLab's client credentials into the existing `oauth2-proxy-secrets` (keys `OAUTH2_PROXY_CLIENT_ID` / `OAUTH2_PROXY_CLIENT_SECRET` / `OAUTH2_PROXY_COOKIE_SECRET`). The charts read the existing secret; no new channel is added.

Configure the `oauth2Proxy` block identically in both the tenant-api and da-portal charts (single SSO):

```yaml
oauth2Proxy:
  provider: gitlab
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

Groups are resolved **at login** and cached in the session cookie. **When `cookieRefresh` is set**, the session re-validates the account against GitLab and **overwrites the groups** every interval (the gitlab provider's `RefreshSession` behavior) — so the group/account revocation lag is **bounded by `cookieRefresh` (not `cookieExpire`)**: a short `cookieRefresh` (e.g. 1h) tightens it, and a user removed from a group or disabled is invalidated at the next refresh. **Without `cookieRefresh`**, groups are frozen as of login until `cookieExpire`. Either way this is a **mitigation, not instant revocation** — the backend still blindly trusts `X-Forwarded-Groups`; true instant revocation requires the identity-hardening work (backend stops trusting the unverified header, see the platform security roadmap), so **production cutover waits for it**.

## Troubleshooting

- **Login loop / cookie not sent**: `cookieSecure: true` requires HTTPS. Either add TLS to the ingress or temporarily set `false` (testing only).
- **Redirect URI mismatch**: the GitLab application's Redirect URI must match `redirectUrl` verbatim (including scheme and the `/oauth2/callback` suffix).
- **Blank screen after login**: usually the `_rbac.yaml` group names are not GitLab full paths (see Step 3).
- **4xx / `431 Request Header Fields Too Large` after login**: for users in **many GitLab groups** (including inherited ones), the session cookie (which encodes the groups and is sent by the client on every request) and the oauth2-proxy-injected `X-Forwarded-Groups` header can grow long enough to exceed the default header/buffer limit of the **ingress controller** or the **nginx inside da-portal**, and the request is dropped. Raise that layer's buffers (nginx-ingress: `nginx.ingress.kubernetes.io/proxy-buffer-size` + `large_client_header_buffers`), or narrow the login surface with `gitlabGroups`. (The tenant-api side is a Go app with a generous `MaxHeaderBytes`, so it is less likely to hit this.)
- **Allow only specific groups**: use `gitlabGroups` (blocks login at the proxy); fine-grained authorization is still decided by `_rbac.yaml`.
