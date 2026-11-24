---
title: "ADR-027: tenant-api establishes caller identity only inside its own pod; human identity is never relayed over the network"
tags: [adr, security, tenant-api, identity, rbac, GHSA-3g2h-rf85-5rrv]
audience: [platform-engineers, contributors]
version: v2.9.0
lang: en
---

# ADR-027: tenant-api establishes caller identity only inside its own pod; human identity is never relayed over the network

> **Language / 語言：** [中文](./027-tenant-api-caller-identity.md) | **English (Current)**

## Status

✅ **Accepted** (2026-11-13, approved by the owner). Every clause of §9 has a check; see §Implementation.

Revised 2026-11-05: oauth2-proxy runs with its structured configuration file, and tenant-api decides which headers to trust from that same file (§2, §7, §10).

Revised 2026-11-07: environment and domain tags are changed only by a platform admin restricted on no scope axis, judged under the writer lock; the offline check also lists tenants whose tags cannot be read (§7).

Revised 2026-11-13: the clauses of §9 have fixed ids, and §Implementation is generated from markers in the code.

Revised 2026-11-22: §9 item 8a also covers references written as a link or broken across lines.

Revised 2026-11-24: rewritten to describe current behavior; the decision is unchanged.

## Decision

**Caller identity is established only inside tenant-api's own pod.** Its only source is the headers that the oauth2-proxy in the same pod sends after login, over a pod-internal Unix domain socket (UDS: a socket between processes on the same host that never touches the network). tenant-api's TCP port carries no identity and serves only probes and metrics; no component relays human identity over the network.

1. Identity is established only inside the pod; its only source is the same pod's oauth2-proxy (§1).
2. How identity is obtained is decided by the listener the connection arrived on, not by the request content (§2).
3. TCP serves only metrics and health checks, not the API (§3).
4. There are no machine callers today; the approach and the trigger for when one appears are settled in advance (§4).
5. The whole deployment has a single oauth2-proxy; the portal and recipe-preview live in tenant-api's pod (§5).
6. The changes that reject old configuration take effect in one breaking release, with no soak period and no switches (§6).
7. Scope axes are always enforced with no shadow mode; an offline check lists affected tenants before upgrading (§7).
8. These decisions are expressed as tests and lints; this document only points to them (§9).

### Example: the listener a request arrives on decides the result

The local trial environment's oauth2-proxy configuration file (tenant-api mounts the same file) declares which header carries the email and the groups, and routes `/api/` to tenant-api's socket:

```yaml {.quote src="try-local/oauth2-proxy/oauth2-proxy.yaml"}
injectRequestHeaders:
- name: X-Forwarded-Email
  values:
  - claimSource:
      claim: email
- name: X-Forwarded-Groups
  values:
  - claimSource:
      claim: groups
# ...
upstreamConfig:
  upstreams:
  - id: api
    path: /api/
    uri: unix:///run/ta-human/human.sock
```

The same request gets a different result through oauth2-proxy onto the UDS than straight at tenant-api's TCP port:

| Request | Listener | Result |
|---|---|---|
| `GET /api/v1/me`, through oauth2-proxy | UDS | identity taken from the `X-Forwarded-Email` / `X-Forwarded-Groups` oauth2-proxy injected, authorized per `_rbac.yaml` |
| `GET /api/v1/me` | TCP | 404: there is no API on TCP |
| `GET /health` with `X-Forwarded-Email: someone@example.com` | TCP | 400: an identity header on TCP is always refused, whatever the path |
| `GET /health` | TCP | 200 |

The three TCP rows are asserted one by one in `components/tenant-api/cmd/server/tcp_router_test.go` (§9 item 1).

### 1. Identity is established only inside tenant-api's pod

The only identity source tenant-api accepts is the headers sent by the oauth2-proxy in the same pod after login, over the pod-internal UDS. A component outside the pod cannot assert identity on a user's behalf, whether or not it also sits behind an oauth2-proxy. There is no machine identity today (§4).

**Trade-off**: oauth2-proxy becomes the sole verifier of human identity, so compromising it compromises identity; in exchange, whether identity can be trusted no longer depends on the cluster's network configuration.

### 2. The listener decides the identity source

- UDS listener: identity is built from headers.
- TCP listener: no identity; it only serves the endpoints in §3. A TCP request carrying any identity header (`X-Forwarded-Email`, `X-Forwarded-Groups`, claim headers injected in oauth2-proxy's configuration file) gets 400 whatever its path, so an old configuration still relaying identity fails immediately instead of being silently ignored.
- Which listener a connection came from is stamped by the server when it accepts the connection; a request cannot rewrite it.
- Everything that needs the caller's identity (including rate limiting and the request log) reads the identity object, never the headers directly.
- The development-mode auth bypass injects the identity object directly, not headers.
- The UDS is not optional: without a socket path or an oauth2-proxy configuration file, tenant-api does not start. Within the pod only the tenant-api and oauth2-proxy containers mount the socket.
- Which headers are trusted is decided by the configuration file oauth2-proxy actually runs with, not by a separate list: the same file is mounted into both containers, and at startup tenant-api reads its `injectRequestHeaders` to learn which header carries the email, the groups and each claim. For every header it injects, oauth2-proxy by default first strips any value of that name sent by the client.
- tenant-api accepts only a configuration file whose meaning is unambiguous, and refuses to start otherwise:
  - Each identity header comes from exactly one session claim (email from `email`, groups from `groups`, the rest from additional claims), with no prefix, no conversion to basic auth, no fixed value (`secretSource`), no keeping of the client's value (`preserveRequestValue`), and header-name normalization left on; a file that injects no email or no groups is refused too.
  - oauth2-proxy expands environment-variable references in the file first and matches field names case-insensitively; tenant-api's reading must be the same, so a file containing `$` or a field name not spelled the way oauth2-proxy spells it is refused. YAML aliases, merge keys and multi-document files are refused too.
- Login-skipping routes must not cover `/api/`: for a request that qualifies to skip login (`--skip-auth-route`-type flags, trusted source IPs, preflight pass-through), if it carries a valid session oauth2-proxy still injects the identity headers, and skips the email-domain and group restrictions. These flags are not in the configuration file; the chart's render test guards them (§9 item 3).

**Trade-off**: one more mandatory socket and shared volume, and tenant-api must parse the small part of oauth2-proxy's configuration file that concerns header injection; in exchange, where human identity may be asserted becomes a topology fact rather than a setting, and "what the proxy injects" and "what tenant-api trusts" are the same file in every way of deploying.

### 3. The TCP plane

- TCP registers only root-level `/health`, `/ready` and `/metrics`, matched exactly (a trailing `/`, or a same-named path under the API, does not count); everything else is 404. The API is not on TCP.
- The auth bypass stays within the four-layer containment of [ADR-022](./022-dev-auth-bypass-four-layer-containment.md) and is mounted only on the UDS plane.
- The local trial environment and the contract tests use the same topology: oauth2-proxy in front, reaching tenant-api over a shared socket; oauth2-proxy exempts every request from login and injects no identity header, and identity is injected by the auth bypass on the UDS plane. No deployment mode skips oauth2-proxy.

### 4. Machine callers: none today

- threshold-govern only produces a read-only report: it does not call tenant-api, opens no PR and changes no file. Its write path (`--apply`), its CronJob and the related alert are removed.
- **Trigger**: the first machine caller that needs to call tenant-api appears, and its image passes the image-pin capability check (it can actually run).
- **Approach at that point** (chosen, not implemented before the trigger): add API routes on TCP that accept only ServiceAccount tokens with audience tenant-api, verified with the existing TokenReview authenticator from `k8s.io/apiserver`; take only the username and discard the groups TokenReview attaches; write permissions in a machine-subject field of `_rbac.yaml`, matched exactly on the full username, with human groups forbidden from starting with `system:`. Any Pod can request such a token for itself, so safety comes from whether the subject is authorized, not from holding a token.
- Why nothing is built before the trigger: building an identity plane for a caller that does not exist adds TokenReview permissions, a cache, schema and checks that have no consumer.
- **Federation tokens**: issuing, listing and revoking them are human operations by tenant admins, done in the portal's Tenant Manager (§5). A deployment that needs periodic automatic reissuance is a machine caller and falls under the trigger above; until then the project ships no non-browser tool to issue them.

### 5. Human identity is not relayed over the network

The whole deployment has a single oauth2-proxy, in tenant-api's pod, routing by path (§10):

- The `/api/` prefix → tenant-api's UDS. Every backend call the portal makes is under this prefix.
- Everything else → the portal static-file container in the same pod. That container listens only on the pod's loopback, does not read identity headers and does not forward requests anywhere.
- Unauthenticated or session-expired requests under `/api/` get 401 (oauth2-proxy's `--api-route`) instead of a redirect to the IdP; on 401 the portal sends the browser to sign in instead of showing offline or demo data.
- "Simulate not provided" is answered 501 by tenant-api, with the same `code` and `error` text da-portal's nginx used to return (tenant-api's error responses also carry `request_id`).

The deployment shape that follows:

- **da-portal**: the chart has only the static, no-login deployment (Tier-1), with no oauth2-proxy, Ingress or reverse proxy.
- **recipe-preview**: lives in tenant-api's pod and provides pure computation only over a pod-internal UDS, like the generator in [ADR-036](./036-single-parser-effective-config.md). The browser calls tenant-api's `POST /api/v1/tenants/{id}/recipe-preview`; tenant-api authorizes the caller on that tenant and then calls recipe-preview; recipe-preview neither receives nor relays any identity, and has no network port.
- There is no "relayer" identity class and no relay list.
- The deployment has one Ingress, one redirect URL and one session cookie; there are no login settings to keep in sync by hand across two charts.

**Trade-off**: tenant-api's pod gains two containers that share its resources, and the portal image version is pinned in the tenant-api chart; in exchange, user identity only ever crosses the single oauth2-proxy → UDS hop, and "who may log in" has one point of decision.

### 6. One-shot cutover, no soak

- The changes that reject old configuration (TCP no longer serving the API, removal of relaying and switches, enforced scope axes) ship together in a single breaking release, and all affected components are upgraded together. The release notes state which old configurations are rejected after upgrading.
- No switch exists for auditing, relaying, "whether the UDS is enabled", "whether oauth2-proxy is enabled", scope-axis shadow mode or open mode. A render test asserts the chart keys do not exist, and values that still set them fail the render; tenant-api's corresponding environment variables are not in its known list, so an old configuration still setting them makes tenant-api refuse to start through the "unknown `TA_*` variable" check (§9).
- Rationale: very few adopters; audit and shadow phases can show neither the absence of forgery nor that nobody is affected.

### 7. Authorization scope model

Roles come from IdP groups; scope (which tenants, environments, domains, organisations) comes from verified identity attributes.

- **Which attributes may be used for authorization**: only claims that oauth2-proxy extracted from a signed token and that the user cannot change. Claim headers are read only on the UDS, and only those injected in oauth2-proxy's configuration file are recognized (§2); tenant-api has no separate claim-header setting, and an `_rbac.yaml` naming a claim the file does not inject fails to load. In oauth2-proxy only OIDC-type providers (including GitLab) extract additional claims; the GitHub provider does not, so a GitHub deployment can scope access by group only.
- **A claim's name is its source**: tenant-api cannot tell apart values two sources write under the same claim name, so a deployment must not map different sources onto one claim name.
- **Scope-determining data is platform-controlled**:
  - The organisation-to-tenant mapping lives in `_tenant_orgs.yaml` in conf.d; it has no write API and is changed through git.
  - Environment and domain tags live in each tenant file's `_metadata`, and only a platform admin restricted on no scope axis may change them through the API — a rule granting admin on `tenants: ["*"]` with no organisation, environment or domain scope. A write by anyone else that adds, changes or removes a tag gets 403.
  - Rationale: a tag decides which rules cover the tenant, so retagging changes the authorization structure, with the same effect as editing `_rbac.yaml`, which the API offers no way to do. An admin restricted to some environments could otherwise move a tenant between the environments it administers and so hand it to every rule scoped to the destination; the scope checks on the state before and after the write do not stop that, since both states are inside its scope.
  - Whole-file writes and batches both go through this check, which only compares the tags before and after the write. A batch cannot set `_metadata` directly, but changing `_profile` to a profile that carries other tags counts as changing them.
  - A tenant file whose tags cannot be read (it does not parse, or its `_metadata` does not decode): no write can be shown to keep its tags, so only such an admin is let through; a broken file is repaired by such an admin or through git. A tenant file that is itself an unresolvable symlink, or that parses but does not declare the tenant, holds no tags to keep and counts as no file yet.
  - The write scope and the tags are judged once more under the writer lock, on the tree the write actually lands on (the serving configuration tree in direct-write mode, the latest base the branch is cut from in PR mode). Between the check outside the lock and taking the lock, a write queued ahead may already have changed the tenant file, so the check outside the lock can only reject early.
- **Always enforced, no shadow mode**: read scope, write scope and organisation scope are each judged independently, with no "record but don't block" option. Untagged tenants are invisible and unwritable under rules that carry a scope restriction.
- **An offline check lists affected tenants before upgrading**: `tenant-api config check --kind scope` reads `_rbac.yaml`, the organisation mapping and the tenant configuration tree, and lists, for each scope axis, "scope-restricted rules × the tenants they therefore do not grant": an untagged tenant needs its tag added; a tenant whose tags cannot be read (a broken tenant file) does not appear in the tenant list under any rule restricting environment or domain, and needs its file fixed. It reads and judges with the same functions as the server. It reads only configuration, so its result is fixed and independent of traffic; run it with the new image before upgrading, and CI runs it against the sample configuration in the repo.
- **No open mode**: when `_rbac.yaml` is not configured, or contains no rules, all access is denied; there is no "authenticated means readable" mode and no switch that restores it.
- **Reverse query**: given a tenant, list the rules that can touch it and under which conditions, for audit; it takes no part in authorization decisions.

Groups are matched as the IdP sends them (for GitLab, the full path, e.g. `org/subteam`), without a provider prefix; a prefix is introduced when one deployment connects a second IdP (trigger in §Alternatives considered).

### 8. Defense in depth that remains

- NetworkPolicy stays, but is no longer the only control; the safety of this ADR does not depend on whether the CNI enforces it.
- oauth2-proxy fetches groups at login and keeps them in the session; after a user is removed from a group, permissions update only when the session is refreshed or expires (providers differ on whether a refresh re-fetches groups). The chart's `oauth2Proxy.cookieExpire` / `oauth2Proxy.cookieRefresh` can shorten that window.

### 9. Decisions expressed as tests and lints

This document is explanation; the checks below are the single source of truth for these decisions. Each clause has a fixed id (for example `3b`). A test or CI step that implements a clause carries the marker `adr-check: ADR-027/9.3b` on the line above it; §Implementation is generated from these markers, and a clause no marker names is listed as having no check yet.

1. TCP plane (§2, §3):
   - **1a** TCP registers only the paths enumerated in §3 (exact match).
   - **1b** Every other path returns 404.
   - **1c** A TCP request carrying any identity header gets 400.
2. Identity is built only on the UDS plane, expressed as architecture tests (§2):
   - **2a** Building identity from headers happens only on the UDS listener's path.
   - **2b** Rate limiting, the request log and the auth bypass never read identity headers.
3. Chart render (§2, §5):
   - **3a** The deployment has exactly one oauth2-proxy.
   - **3b** Unauthenticated `/api/` requests get 401.
   - **3c** Only tenant-api and oauth2-proxy mount the human socket.
   - **3d** The UDS always exists.
   - **3e** oauth2-proxy and tenant-api mount the same configuration file.
   - **3f** oauth2-proxy in a production deployment has no login-skipping condition that covers `/api/` (the development topology of §3 is contained per ADR-022 and is exempt).
   - **3g** The nginx configuration the portal container actually loads (whether from the chart or the image) contains no forwarding.
4. Portal (§5):
   - **4a** Every backend path called from the portal source is under the `/api/` prefix.
   - **4b** The simulate path returns 501 with the same `code` and `error` text da-portal's nginx used to return.
5. No identity relay, and the removed settings are gone (§4, §5, §6):
   - **5a** A scan of helm, k8s manifests and rule packs: no configuration relays identity headers over the network to any component.
   - **5b** In the same scan, the chart keys deleted in §6 do not exist.
   - **5c** In the same scan, threshold-govern's CronJob, its NetworkPolicy allowance and its alert do not exist.
   - **5d** The environment variables deleted in §6 are not in tenant-api's known list.
6. Authorization scope (§7):
   - **6a** tenant-api refuses to start when oauth2-proxy's configuration file does not have the shape in §2.
   - **6b** Parsing refuses the wrong shapes, with fixtures covering at least: keeping the client's value, a fixed value, a prefix, the wrong claim, missing email or groups, environment-variable references, and non-canonical field-name spellings.
   - **6c** An `_rbac.yaml` with no rules denies all access.
   - **6d** No switch exists that restores open mode.
   - **6e** A write by a non-platform-admin that changes a tenant's environment or domain tags, or overwrites a tenant file whose tags cannot be read, is rejected.
   - **6f** The 6e check and the write scope are made under the writer lock, on the tree the write lands on.
7. Offline check (§7):
   - **7a** `tenant-api config check` lists untagged tenants and tenants whose tags cannot be read per scope axis and exits non-zero, tested against a fixed fixture.
   - **7b** It agrees with the server's tenant-list decision, rule by rule.
   - **7c** CI runs it against the sample configuration in the repo.
8. Reference check (this ADR and [#1667](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1667)):
   - **8a** Every reference written as `ADR-NNN §section` in the repo (and sections chained after it on the same line) must resolve to an existing section of that ADR (matched by the section heading's number or name). A reference with `ADR-NNN` written as a link and followed by `§section`, or with the section as the link text, or with `ADR-NNN` ending a line and the next line, its comment prefix removed, opening with `§section`, is the same reference, and the sections chained after it on that next line must resolve too.
   - **8b** `ADR-027` followed by a section id used by the withdrawn 2026-07 draft (the list is in the check itself) always fails.
9. oauth2-proxy configuration check (§10):
   - **9a** CI runs oauth2-proxy's configuration check (`--config-test`) with the image pinned by the chart (the same digest) against the configuration file the chart generates.
   - **9b** CI uses the same version of the oauth2-proxy library to assert that routing matches the example in §10.

### 10. oauth2-proxy routing configuration

oauth2-proxy runs with its structured configuration file (`--alpha-config`), in which each upstream's path and destination are set separately. The configuration file the chart generates has exactly these two upstreams (plus header injection, the provider and the listener address); the tests of §9 item 9 read this block and the table below straight out of the Chinese version of this ADR and check them with the same oauth2-proxy library version the chart pins:

```yaml
upstreamConfig:
  upstreams:
  - id: api
    path: /api/
    uri: unix:///run/ta-human/human.sock
  - id: portal
    path: /
    uri: http://127.0.0.1:8081
```

Routing result:

| Request path | Goes to |
|---|---|
| `/api/v1/me?x=1` | UDS, path and query unchanged |
| `/api/` | UDS |
| `/api`, `/apix` | portal |
| `/`, `/index.html` | portal |

- **Legacy flags cannot do it**: the route path of `--upstream=unix://…` is fixed at `/`, so the API cannot be placed at `/api/`; the portal is at `/` as well, the two collide, and configuration validation fails (`multiple upstreams found with path "/"`).
- With the structured configuration file, the provider, header injection and listener address live in the file, and the corresponding legacy flags can no longer be used alongside it (with them, oauth2-proxy fails to parse its flags and refuses to start); other flags such as session, cookie and `--api-route` stay as they are.
- oauth2-proxy marks the structure of this file as subject to change between minor versions. The file is parsed strictly, and oauth2-proxy refuses to start on a field it does not recognize, so a structural change fails immediately on upgrade; §9 item 9 makes it fail in CI.

## Context

tenant-api's RBAC, `/me`, federation token issuance and the author of git writes all depend on the caller's identity, which comes from `X-Forwarded-Email` / `X-Forwarded-Groups` and any configured claim headers. These headers are unsigned and tenant-api cannot verify them itself; the only premise that makes them trustworthy is that whoever sends them is the oauth2-proxy that performed the login.

In the latest released tenant-api (2.9.8) that premise does not hold:

- tenant-api reads identity from headers on TCP port 8080, and the same pod's oauth2-proxy also sends requests in over `localhost:8080`, so tenant-api cannot tell it apart from callers on the network.
- da-portal's nginx forwards the `X-Forwarded-Email` the browser sent on to tenant-api.
- Who can reach 8080 is restricted only by NetworkPolicy (its allowances include the threshold-govern CronJob).

So anything that can reach 8080, or reach a component that forwards these headers, can assert someone else's identity, including platform admin; on a CNI that does not enforce NetworkPolicy there is no defense. This is the root cause of [GHSA-3g2h-rf85-5rrv](https://github.com/vencil/Dynamic-Alerting-Integrations/security/advisories/GHSA-3g2h-rf85-5rrv); the fix that advisory lists only tightens, at the network layer (L4), who can reach 8080.

The main human entry point is the relay through da-portal, and what it relays is the **user's** identity. Adding a workload token to each relaying component can only prove that the relaying component is genuine, not that the user it relays is. So this ADR does not harden relaying; it removes it: identity is established only inside tenant-api's own pod.

## Consequences and known limitations

- **Breaking** (these changes are not released yet; the latest released tenant-api is 2.9.8; they ship together in one release, §6):
  - Requests that still relay identity through da-portal or recipe-preview, or call the API over TCP, are rejected; the affected components must be upgraded together.
  - The login-protected portal is served by the tenant-api deployment; the da-portal chart keeps only the static deployment.
  - threshold-govern only produces a read-only report; `--apply` and its related flags, the CronJob and the alert are removed.
  - Federation tokens are issued through the portal.
  - An unconfigured or rule-less `_rbac.yaml` does not allow reads.
  - Scope axes are always enforced: untagged tenants are invisible under scope-restricted rules; writes are constrained by environment and domain scope, so writes to tagged tenants outside the scope are rejected too; the organisation scope axis is new. List them with `tenant-api config check --kind scope` and tag them before upgrading.
  - Only a platform admin restricted on no scope axis can change environment and domain tags, or overwrite a tenant file whose tags cannot be read, through the API; in a deployment whose admins are all restricted on environment or domain, tags and such broken files can be changed only through git.
  - The local trial environment (try-local) and the contract tests put oauth2-proxy in front; tenant-api's probe port there is 8083.
- oauth2-proxy is the sole verifier of human identity: its image is pinned by digest.
- The structure of the structured configuration file may change between oauth2-proxy minor versions; upgrading oauth2-proxy may require updating the configuration file the chart generates and the part tenant-api parses (§10).
- The GitHub provider supplies no additional claims, so scope in such deployments can be decided by group only (§7).
- When two sources write under the same claim name, tenant-api cannot tell them apart (§7); a true issuer namespace waits for the deferred item of verifying users' JWT access tokens.
- tenant-api's pod carries the API, the portal and recipe-preview, which scale together.
- A user removed from a group keeps permissions until the session updates (§8).
- GHSA-3g2h-rf85-5rrv currently lists 2.9.8 as the patched version; once this ADR's changes are released, the advisory's patched version and description need updating.

## Alternatives considered

- **Authenticate every hop, then enforce after an audit phase** (each component relaying identity to tenant-api attaches its own workload token, first only recording, then blocking after a multi-week soak): for a component relaying user identity, a token proves only the component; every additional relaying component adds another set of settings that must all be right together (token audience, namespace, Pod labels, NetworkPolicy, image version, switches); an audit phase can only show that legitimate callers carry tokens, not that nobody forges.
- **Keep the external surface, just switch audit to enforce**: still trusts the user identity relayed by da-portal and recipe-preview, which shrinks the problem from "any Pod that can reach tenant-api" to "any Pod that can reach da-portal's internal port" without removing it.
- **da-portal forwarding the browser's session cookie to tenant-api's oauth2-proxy (two proxies sharing a session)**: still relays a human credential over the network; the cookie secret lives in two namespaces and cannot be checked mechanically for consistency; "who may log in" has two points of decision.
- **The Ingress routing `/api/` by path to tenant-api's oauth2-proxy, with the portal served without login**: an Ingress cannot point at a Service in another namespace, so one host needs two Ingresses merged by the controller; host, redirect URL and TLS must be paired by hand across charts. §7's claim-header injection needs the structured configuration file as well, so this route would not avoid it.
- **The portal's nginx in front inside the pod, passing `/api/` to the same pod's oauth2-proxy (to avoid the structured configuration file)**: forwarding reappears in the portal container, which defeats §9 item 3; §7's claim headers still need the structured configuration file, so both costs are paid.
- **The chart generating both the oauth2-proxy configuration and tenant-api's claim-header setting from one set of values**: only Helm deployments stay mechanically consistent; the local-trial compose file would need a second hand-written copy.
- **A separate tenant-api flag declaring which headers oauth2-proxy overwrites**: the flag is itself another setting to keep in sync by hand, and tenant-api cannot detect when it is wrong.
- **tenant-api serving the portal's static files itself**: CSP, caching and SPA fallback would have to be rebuilt in Go, and the portal and tenant-api version lines would be tied together.
- **Fixing threshold-govern's write path and building machine identity now**: it means building a whole identity plane for the only machine caller. The trigger is in §4.
- **Keeping shadow mode for scope axes (off by default)**: "would deny" counts only move with traffic and cannot show that nobody is affected; tenants can be enumerated, so a deterministic offline check is more reliable (Gatekeeper and Kyverno also rely on background scans for enumerable resources, using traffic-driven dry-run only for things that cannot be enumerated).
- **Using the reverse query as the pre-upgrade check**: it looks up one tenant at a time, needs an admin session, and does not evaluate the metadata axis.
- **Keep recipe-preview where it is and have it verify tenant-api via TokenReview**: it still needs a network port and one more set of verification settings; moving it into the pod removes that set.
- **oauth2-proxy verifying machine tokens via `--extra-jwt-issuers`**: local signature verification has no revocation; the audience of a ServiceAccount token is chosen by the requester, so the subject must still be matched. Revisit when the §4 trigger fires.
- **A kube-rbac-proxy sidecar, or authorizing machines via SubjectAccessReview**: machine permissions would live in Kubernetes RBAC, a second source of authorization that the reverse query cannot see.
- **Relaying a user token (RFC 8693 token exchange, on-behalf-of)**: GitHub and GitLab CE access tokens are opaque, so there is no verifiable user token to relay; for OIDC customers that issue JWT access tokens, tenant-api can verify them directly (see the deferred item below).
- **Using the OIDC ID token as an API credential**: an ID token's audience is the OAuth client, not the API, and it can be replayed across services; the established guidance is that it should not be sent to APIs ([oauth.net](https://oauth.net/id-tokens-vs-access-tokens/)).
- **Verifying the user's JWT access token**: deferred. Trigger: a customer IdP that issues JWT access tokens with audience tenant-api and groups included ([RFC 9068](https://datatracker.ietf.org/doc/html/rfc9068)). It would be added as a new identity source on the UDS plane, without changing §1–§5.
- **Trusting a loopback source IP**: other containers in the same pod are loopback too; middleware that derives the source IP from headers can be spoofed.
- **HMAC-signed headers**: distributing and rotating the keys is on us, which amounts to building a weaker ServiceAccount token ourselves.
- **SPIFFE/SPIRE, service mesh**: running an entire identity plane for a handful of callers. Trigger: cross-cluster or non-Kubernetes machine callers, or a requirement for channel-bound identity.
- **Identity broker (Dex / Keycloak)**: Dex's access tokens are opaque; Keycloak can issue verifiable access tokens but needs a database and ongoing patching. Trigger: one deployment needs several login sources.
- **Provider prefixes on GitLab / GitHub groups**: no collisions with a single provider. Trigger: one deployment connects a second IdP.

## Implementation

Generated by `scripts/tools/dx/gen_adr_checks.py` from the `adr-check:` markers in the code; do not edit by hand. After changing a marker, run `python3 scripts/tools/dx/gen_adr_checks.py --write`. Paths are relative to the repository root; the parentheses name the test or CI step under each marker.

<!-- BEGIN GENERATED: adr-checks section=9 -->
- **1a**: `components/tenant-api/cmd/server/tcp_router_test.go` (`TestTCPRouter_RegistersOnlyTheProbeRoutes`)
- **1b**: `components/tenant-api/cmd/server/tcp_router_test.go` (`TestTCPRouter_EverythingElseIs404`)
- **1c**: `components/tenant-api/cmd/server/tcp_router_test.go` (`TestTCPRouter_IdentityHeaderIs400`)
- **2a**: `components/tenant-api/internal/rbac/devbypass_test.go` (`TestResolveIdentity_IgnoresHeadersOnTCP`); `components/tenant-api/internal/rbac/identity_arch_test.go` (`TestHeaderResolutionOnlyInResolveIdentity`, `TestIdentityHeaderNamesOnlyInTheirOwners`)
- **2b**: `components/tenant-api/internal/rbac/identity_arch_test.go` (`TestRateLimitLogAndBypassReadNoRequestHeader`)
- **3a**: `tests/helm/test_identity_single_hop.py` (`test_no_raw_manifest_runs_an_oauth2_proxy`, `test_the_only_oauth2_proxy_is_in_the_tenant_api_pod`)
- **3b**: `tests/oauth2-proxy/chart_test.go` (`TestChartAPIRouteIsTheSameInEveryVariant`); `tests/oauth2-proxy/image_config_test.sh`
- **3c**: `tests/helm/test_human_socket_guard.py` (`test_render_default_points_upstream_at_socket`); `tests/helm/test_tenant_api_portal_container.py` (`test_portal_container_mounts_no_identity_or_secret`)
- **3d**: `components/tenant-api/cmd/server/tcp_router_test.go` (`TestCheckIdentityPlaneFlags`); `tests/helm/test_human_socket_guard.py` (`test_deployment_wires_the_socket`, `test_render_default_points_upstream_at_socket`, `test_render_without_proxy_fails`, `test_socket_path_is_a_chart_constant`)
- **3e**: `tests/helm/test_oauth2_proxy_alpha_config.py` (`test_both_containers_read_the_same_file`)
- **3f**: `tests/oauth2-proxy/chart_test.go` (`TestChartConfig`, `TestChartOAuth2ProxyEnvIsAllowlisted`)
- **3g**: `tests/helm/test_tenant_api_portal_container.py` (`test_portal_runs_the_charts_config_and_it_forwards_nothing`)
- **4a**: `tools/portal/tests/api-fetch.test.ts` (`apiFetch: the path`); `tools/portal/tests/network-calls-only-in-api-fetch.test.ts` (`network calls only in apiFetch`)
- **4b**: `components/tenant-api/cmd/server/simulate_route_test.go` (`TestSimulateNotProvided`); `tests/shared/test_portal_simulate_not_provided.py` (`test_tenant_api_answers_the_same_body`, `test_tenant_api_answers_the_same_code`)
- **5a**: `tests/helm/test_identity_single_hop.py` (`test_no_raw_manifest_or_rule_pack_names_an_identity_header`, `test_no_rendered_object_but_the_oauth2_proxy_config_names_an_identity_header`)
- **5b**: `tests/helm/test_tenant_api_machine_identity_removed.py` (`test_a_removed_key_fails_the_render`, `test_values_yaml_carries_no_removed_key`); `tests/helm/test_tenant_api_scope_switches_removed.py` (`test_a_removed_key_fails_the_render`, `test_values_yaml_carries_no_removed_key`)
- **5c**: `tests/shared/test_threshold_govern_write_path_removed.py` (`test_no_chart_mentions_threshold_govern`, `test_no_network_policy_admits_the_threshold_govern_pod`, `test_no_threshold_govern_cronjob`, `test_no_threshold_governance_stale_alert`, `test_tenant_api_chart_has_no_threshold_govern_allowance`)
- **5d**: `components/tenant-api/cmd/server/env_check_test.go` (`TestCheckTAEnv_RefusesSettingsADR027Removed`)
- **6a**: `components/tenant-api/cmd/server/identity_config_startup_test.go` (`TestMainRefusesAnAmbiguousOAuth2ProxyConfig`)
- **6b**: `components/tenant-api/internal/proxyconfig/proxyconfig_test.go` (`TestLoad_RefusesAmbiguousShapes`, `TestParse_Refusals`)
- **6c**: `components/tenant-api/internal/rbac/empty_config_mode_test.go` (`TestEmptyRuleSet_EveryEntryPointDenies`, `TestEmptyRuleSet_LegacyShimsDeny`)
- **6d**: `components/tenant-api/cmd/server/env_check_test.go` (`TestCheckTAEnv_RefusesSettingsADR027Removed`); `tests/helm/test_tenant_api_scope_switches_removed.py` (`test_a_removed_key_fails_the_render`, `test_the_default_render_sets_no_removed_variable`, `test_values_yaml_carries_no_removed_key`)
- **6e**: `components/tenant-api/internal/handler/label_guard_test.go` (`TestLabelGuardBatchDirect`, `TestLabelGuardPutDirect`, `TestLabelGuardPutNewTenant`, `TestLabelGuardPutPRModeUnreadableBase`, `TestLabelGuardUnreadableLabelsBefore`)
- **6f**: `components/tenant-api/internal/handler/fresh_base_scope_test.go` (`TestPRModeOrgScopeJudgedOnFreshBase`, `TestPRModeScopeAuthoritativeOnFreshBase`, `TestPRModeScopeJudgedEvenWhenTheTreeMovedMidRequest`); `components/tenant-api/internal/handler/label_guard_test.go` (`TestLabelGuardBatchDirectRelabelLandsFirst`, `TestLabelGuardBatchPRModeBaseDrift`, `TestLabelGuardPutDirectRelabelLandsFirst`, `TestLabelGuardPutPRModeBaseDrift`, `TestLabelGuardPutPRModeUnreadableBase`, `TestScopeGateDirectNoopFirstOpStillJudged`, `TestScopeGateDirectRelabelLandsFirst`)
- **7a**: `components/tenant-api/cmd/server/scope_check_test.go` (`TestConfigCheckScope_Fixture`)
- **7b**: `components/tenant-api/internal/handler/scope_gap_planes_test.go` (`TestScopeGapsMatchEveryPlane`)
- **7c**: `.github/workflows/ci.yml` (`Scope check on the try-local sample config (ADR-027 §7)`)
- **8a**: `tests/lint/test_check_adr_section_refs.py` (`test_a_broken_reference_is_reported_on_the_line_with_the_adr`, `test_a_section_not_continuing_the_reference_is_not_its_section`, `test_existing_sections_pass`, `test_link_and_line_break_references_are_checked`, `test_missing_sections_fail`, `test_the_repository_is_clean`)
- **8b**: `tests/lint/test_check_adr_section_refs.py` (`test_draft_code_needs_adr_027_before_it`, `test_withdrawn_draft_codes_fail`)
- **9a**: `.github/workflows/ci.yml` (`Run --config-test in the chart's oauth2-proxy image (by digest)`); `tests/oauth2-proxy/image_config_test.sh`
- **9b**: `tests/oauth2-proxy/chart_test.go` (`TestChartConfig`); `tests/oauth2-proxy/routing_test.go` (`TestADRSection10Routing`); `tests/oauth2-proxy/version_test.go` (`TestLibraryVersionIsTheChartImage`)
<!-- END GENERATED: adr-checks -->

## Related

- [#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040): implementation and release tracking
- [GHSA-3g2h-rf85-5rrv](https://github.com/vencil/Dynamic-Alerting-Integrations/security/advisories/GHSA-3g2h-rf85-5rrv)
- [ADR-009](./009-tenant-manager-crud-api.md): the original decision to delegate authentication to oauth2-proxy
- [ADR-022](./022-dev-auth-bypass-four-layer-containment.md): containment of the development-mode auth bypass
- [ADR-036](./036-single-parser-effective-config.md): precedent for providing computation over a pod-internal UDS
- [`governance-security.md`](../governance-security.en.md): NetworkPolicy and port 8080 today
