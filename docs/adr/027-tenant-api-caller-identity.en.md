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

🟡 **Proposed** (rewritten 2026-11-04; on the same day the owner settled the three formerly open items: the browser entry point, threshold-govern, and shadow mode for scope axes). Moves to Accepted once the owner approves the full text.

Revised 2026-11-05: §10 now records the test result and adopts oauth2-proxy's structured configuration file (alpha config); tenant-api decides which headers to trust from that same file (§2, §7); the "one-shot" of §6 is limited to the changes that reject old configuration.

Revised 2026-11-07: the offline check of §7 also lists tenants whose tags cannot be read but that a scope-restricted rule applies to, and a broken file no restricting rule applies to no longer affects its exit code; environment and domain tags are changed by a platform admin restricted on no scope axis, and an admin restricted to some environments or domains can no longer change them; a tenant file whose tags cannot be read is likewise overwritten through the API only by such an admin; tags and the write scope are judged under the writer lock, on the tree the write actually lands on, in both write modes.

Revised 2026-11-13: §Implementation lists the file for each check of §9, and the clauses that have no check yet; "simulate returns the original body" in §5 and §9 item 4 now reads "the same `code` and `error` text", as tenant-api's error responses also carry `request_id`.

The reference check (§9 item 8) is in place: references in the repo that cited this ADR by the draft's IDs have been rewritten and the legacy-reference table deleted.

This ADR replaces an unpublished draft from 2026-07. That draft's direction was "authenticate every hop": each component relaying identity to tenant-api would attach its own workload token, first in audit-only mode, then enforced after a multi-week soak. The code merged under that direction is either audit-only or off by default, and none of it is in any released tenant-api image. This ADR withdraws that direction; see §Context and §Alternatives considered. Fixes completed in that period that do not depend on the direction (an empty `_rbac.yaml` denies access; the middleware that derived the source IP from headers is gone) are kept.

Implementation and release are tracked in [#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040).

## Summary

**Problem**: tenant-api decides who the caller is from the `X-Forwarded-Email` / `X-Forwarded-Groups` headers, and those headers can arrive over the network. Anything that can reach tenant-api's TCP API port, or reach a component that forwards those headers to it, can assert someone else's identity, including platform admin. The only thing stopping this today is NetworkPolicy; on a CNI that does not enforce NetworkPolicy there is no defense. This is the root cause of [GHSA-3g2h-rf85-5rrv](https://github.com/vencil/Dynamic-Alerting-Integrations/security/advisories/GHSA-3g2h-rf85-5rrv); the fix released earlier was only an L4 stopgap.

**Decision**:

1. Caller identity is established only inside tenant-api's own pod, and its only source is the oauth2-proxy in the same pod, over a Unix domain socket (§1).
2. How identity is obtained is decided by the listener the connection arrived on, not by the request content (§2).
3. TCP serves only metrics and health checks, not the API (§3).
4. There are no machine callers today; the approach and the trigger for when one appears are settled in advance (§4).
5. No component relays human identity over the network any more. The whole deployment has a single oauth2-proxy; the portal and recipe-preview move into tenant-api's pod (§5).
6. The changes that reject old configuration take effect in one breaking release, with no soak period and no switches (§6).
7. Scope axes are always enforced with no shadow mode; an offline check lists affected tenants before upgrading (§7).
8. These decisions are expressed as tests and lints; this document only points to them (§9).

## Context

### How identity is established

tenant-api's RBAC, `/me`, federation token issuance and the author of git writes consume one identity object, parsed from headers: `X-Forwarded-Email` / `X-Forwarded-Groups`, plus any configured claim headers. Rate limiting and the request log additionally read `X-Forwarded-Email` directly, bypassing that object. The headers are unsigned and tenant-api cannot verify them itself; the only premise that makes them trustworthy is that whoever sends them is the oauth2-proxy that performed the login.

### That premise does not hold today

| Path into tenant-api | Identity carried | What guarantees it |
|---|---|---|
| browser → da-portal nginx → tenant-api TCP | `X-Forwarded-*` forwarded by nginx | NetworkPolicy only |
| browser → da-portal → recipe-preview → tenant-api TCP (permission probe) | user headers forwarded by recipe-preview | NetworkPolicy only |
| threshold-govern CronJob → tenant-api TCP | self-declared group | NetworkPolicy only |
| same-pod oauth2-proxy → tenant-api TCP (UDS not enabled, the current default) | headers set by oauth2-proxy | nothing: tenant-api cannot tell it apart from network callers |
| same-pod oauth2-proxy → tenant-api UDS (optional) | headers set by oauth2-proxy | the pod boundary |

The main human entry point is the first row, not the last. The 2026-07 draft assumed humans took the last row, so moving human traffic onto a UDS and requiring tokens elsewhere would suffice. But rows one and two relay the **user's** identity: a token can only prove that the relaying component is genuine, not that the user it relays is.

### Why "authenticate every hop" is withdrawn

- Every additional component that relays identity adds another set of settings that must all be right together: token audience, namespace, Pod labels, NetworkPolicy, image version, switches. An inventory in 2026-11 found that safety required several such sets to be set correctly together, some kept in sync only by hand.
- An audit phase can only show that legitimate callers carry tokens; it cannot show that nobody forges. A forger just keeps the token and the headers consistent.
- Several PRs were merged in that direction without a single line of the enforce step; and it had only one machine caller to observe (threshold-govern), which has never been able to run on a released image.
- A wrong premise (which path humans take) plus an ever-growing set of pairings means the direction is what needs changing, not another layer.

## Decision

### 1. Identity is established only inside tenant-api's pod

The only identity source tenant-api accepts is the headers sent by the oauth2-proxy in the same pod after login, over a pod-internal Unix domain socket (UDS). A component outside the pod cannot assert identity on a user's behalf, whether or not it also sits behind an oauth2-proxy. There is no machine identity today (§4).

**Trade-off**: oauth2-proxy becomes the sole verifier of human identity, so compromising it compromises identity; in exchange, whether identity can be trusted no longer depends on the cluster's network configuration.

### 2. The listener decides the identity source

- UDS listener: identity is built from headers.
- TCP listener: no identity; it only serves the endpoints in §3. A TCP request carrying any identity header (`X-Forwarded-Email`, `X-Forwarded-Groups`, claim headers injected in oauth2-proxy's configuration file) gets 400, so an old configuration still relaying identity fails immediately instead of being silently ignored.
- Which listener a connection came from is stamped by the server when the connection is set up; a request cannot rewrite it.
- Everything that needs the caller's identity (including rate limiting and the request log) reads the identity object, never the headers directly.
- The development-mode auth bypass injects the identity object directly, no longer headers.
- The UDS is no longer optional: tenant-api refuses to start without it. Within the pod only the tenant-api and oauth2-proxy containers may mount the socket.
- Which headers are trusted is decided by the configuration file oauth2-proxy actually runs with, not by a separate list: the same file is mounted into both containers, and at startup tenant-api reads its `injectRequestHeaders` to learn which header carries the email, the groups and each claim. For every header it injects, oauth2-proxy by default first strips any value of that name sent by the client.
- tenant-api accepts only a configuration file whose meaning is unambiguous, and refuses to start otherwise:
  - Each identity header comes from exactly one session claim (email from `email`, groups from `groups`, the rest from allowed additional claims), with no prefix, no conversion to basic auth, no fixed (secret) value, no keeping of the client's value (`preserveRequestValue`), and header-name normalization left on; a file that injects no email or no groups is refused too.
  - oauth2-proxy expands environment-variable references in the file first and matches field names case-insensitively; tenant-api's reading must be the same, so a file containing environment-variable references or non-canonical field-name spellings is refused.
- Login-skipping routes must not cover `/api/`: for a request that qualifies to skip login (`--skip-auth-route`-type flags, trusted source IPs, preflight pass-through), if it carries a valid session oauth2-proxy still injects the identity headers, and skips the email-domain and group restrictions. These flags are not in the configuration file; the chart's render test guards them (§9 item 3).

**Trade-off**: one more mandatory socket and shared volume, and tenant-api must parse the small part of oauth2-proxy's configuration file that concerns header injection; in exchange, where human identity may be asserted becomes a topology fact rather than a setting, and "what the proxy injects" and "what tenant-api trusts" are the same file in every way of deploying.

### 3. The TCP plane

- TCP registers only the root-level metrics, liveness and readiness endpoints, matched by exact path (same-named paths under the API do not count); everything else is 404. The API is not on TCP.
- The auth bypass stays within the four-layer containment of [ADR-022](./022-dev-auth-bypass-four-layer-containment.md) and may only act on the UDS plane.
- The local trial environment, contract tests and other development setups use the same topology: the in-pod oauth2-proxy runs with login skipped on every path, and identity is injected by the auth bypass on the UDS plane. No deployment mode skips oauth2-proxy.

### 4. Machine callers: none today

- The only machine caller ever planned was threshold-govern's write path (`--apply`), which has never run in any released image. This ADR removes its CronJob, its write path and the related alert; the read-only recommendation (dry-run), which does not go through tenant-api, is kept.
- **Trigger**: the first machine caller that needs to call tenant-api appears, and its image passes the image-pin capability check (it can actually run).
- **Approach at that point** (chosen, not implemented before the trigger): add API routes on TCP that accept only ServiceAccount tokens with audience tenant-api, verified with the existing TokenReview authenticator from `k8s.io/apiserver`; take only the username and discard the groups TokenReview attaches; write permissions in a machine-subject field of `_rbac.yaml`, matched exactly on the full username, with human groups forbidden from starting with `system:`. Any Pod can request such a token for itself, so safety comes from whether the subject is authorized, not from holding a token.
- Why nothing is built before the trigger: building an identity plane for a caller that does not exist adds TokenReview permissions, a cache, schema and checks that have no consumer.
- **Federation tokens**: issuing, listing and revoking them are human operations by tenant admins, done through the portal (§5). A deployment that needs periodic automatic reissuance is a machine caller and falls under the trigger above; until then there is no non-browser way to issue them.

### 5. Human identity is not relayed over the network

The whole deployment has a single oauth2-proxy, in tenant-api's pod, routing by path:

- The `/api/` prefix → tenant-api's UDS. Every backend call the portal makes must be under this prefix, including the former `/preview`.
- Everything else → the portal static-file container in the same pod. That container does not read identity headers and does not forward requests anywhere.
- Unauthenticated or session-expired requests under `/api/` get 401 instead of a redirect to the IdP; on 401 the portal redirects to login instead of switching to demo data.
- The "simulate not provided" 501 that da-portal's nginx used to return is now returned by tenant-api, with the same status code, `code` and `error` text (tenant-api's error responses also carry `request_id`).

Consequent changes:

- **da-portal**: no longer has its own oauth2-proxy, Ingress or reverse proxy. The da-portal chart keeps only the static, no-login deployment (Tier-1).
- **recipe-preview**: moves into tenant-api's pod and provides pure computation only over a pod-internal UDS, like the generator in [ADR-036](./036-single-parser-effective-config.md). The browser calls tenant-api, which authorizes the caller and then calls recipe-preview; recipe-preview neither receives nor relays any identity, and has no network port any more.
- The concept of a "relay" is removed entirely: no relay list, and no "verified relayer" identity class.
- The deployment ends up with one Ingress, one redirect URL and one session cookie; there are no login settings left to keep in sync by hand across two charts.

**Trade-off**: tenant-api's pod gains two containers that share its resources, and the portal image version is pinned in the tenant-api chart; in exchange, user identity only ever crosses the single oauth2-proxy → UDS hop, and "who may log in" has one point of decision.

### 6. One-shot cutover, no soak

- The changes that reject old configuration (TCP no longer serving the API, removal of relaying and switches, enforced scope axes) take effect in a single breaking release, shipping all affected components together. The release notes show what the old configurations that get rejected after upgrading look like.
- Parts of the new topology that are purely additive and reject no old configuration (for example, adding the portal container to tenant-api's pod) may be merged earlier; if a release is cut for other reasons in the meantime, those parts appear in it first, without making any existing deployment less secure.
- All switches about auditing, relaying, "whether the UDS is enabled", "whether oauth2-proxy is enabled", scope-axis shadow mode and open mode are deleted. A render test asserts the chart keys do not exist; tenant-api's environment variables are removed from its known list, so an old configuration still setting them makes tenant-api refuse to start through the existing "unknown `TA_*` variable" check (§9).
- Rationale: very few adopters; audit and shadow phases cannot show the absence of forgery or that nobody is affected; and none of these switches is in any released image, so the soak never actually started.

### 7. Authorization scope model

Roles come from IdP groups; scope (which tenants, environments, domains, organisations) comes from verified identity attributes.

- **Which attributes may be used for authorization**: only claims that oauth2-proxy extracted from a signed token and that the user cannot change. Claim headers are read only on the UDS, and only those injected in oauth2-proxy's configuration file are recognized (§2); tenant-api has no separate claim-header setting. In oauth2-proxy only OIDC-type providers (including GitLab) extract additional claims; the GitHub provider does not, so a GitHub deployment can scope access by group only.
- **Values carry a source namespace**, so same-named values from different sources cannot impersonate each other.
- **Scope-determining data is platform-controlled**: the organisation-to-tenant mapping lives in a configuration layer only platform admins can change. Environment and domain tags currently live in each tenant file's `_metadata` and can be changed through the write API; from now on only a platform admin restricted on no scope axis may change them — a rule granting admin on `tenants: ["*"]` with no organisation, environment or domain scope — and any other write that changes them is rejected. A tag decides which rules cover the tenant, so retagging changes the authorization structure, with the same effect as editing `_rbac.yaml`, which the API offers no way to do. An admin restricted to some environments could otherwise move a tenant between the environments it administers and so hand it to every rule scoped to the destination; the scope checks on the state before and after the write do not stop that, since both states are inside its scope. For a tenant file whose tags cannot be read (it does not parse, or its `_metadata` does not decode), a whole-file write cannot be shown to keep its tags — content without tags may be removing them — so a whole-file write over such a file is likewise limited to such an admin; a broken file is repaired by such an admin or through git. A partial write that changes other fields only (a batch operation that sets no scope field, custom alerts) keeps `_metadata` exactly as it is and is not limited this way. A tenant file that is itself an unresolvable symlink, or that parses but does not declare the tenant, holds no tags to keep and counts as no file yet. The write scope and the tags are judged once more under the writer lock, on the tree the write actually lands on (the serving configuration tree in direct-write mode, the latest base the branch is cut from in PR mode): between the check outside the lock and taking the lock, a write queued ahead may already have changed the tenant file, so the check outside the lock can only reject early.
- **Always enforced, no shadow mode**: read scope, write scope and organisation scope are each judged independently, with no "record but don't block" option. Untagged tenants are invisible and unwritable under rules that carry a scope restriction.
- **An offline check lists affected tenants before upgrading**: `tenant-api config check` (which today only checks that a single file parses) is extended to read `_rbac.yaml`, the organisation mapping and the tenant configuration tree together, and lists, for each scope axis, "scope-restricted rules × the tenants they therefore do not grant": an untagged tenant needs its tag added; a tenant whose tags cannot be read (a broken tenant file) does not appear in the tenant list under any rule restricting environment or domain, and needs its file fixed. It exits non-zero if it finds any; a broken file no restricting rule applies to does not affect the exit code, since the scope axes do not change what it is granted. It reads only configuration, so its result is fixed and independent of traffic; run it with the new image before upgrading, and in CI.
- **No open mode**: when `_rbac.yaml` is not configured, or contains no rules, all access is denied; the "authenticated means readable" mode and the switch that restores it are deleted.
- The reverse query stays: given a tenant, list the rules that can touch it, for audit.

GitLab groups are matched by full path (`org/subteam`) as-is, without a provider prefix; a prefix is introduced when one deployment connects a second IdP (trigger in §Alternatives considered).

### 8. Defense in depth that remains

- NetworkPolicy stays, but is no longer the only control; the safety of this ADR does not depend on whether the CNI enforces it.
- oauth2-proxy fetches groups at login and keeps them in the session; after a user is removed from a group, permissions update only when the session is refreshed or expires (providers differ on whether a refresh re-fetches groups), mitigated by a shorter session lifetime.

### 9. Decisions expressed as tests and lints

This document is explanation; the checks below are the single source of truth for these decisions. §Implementation lists the file for each check.

1. TCP registers only the paths enumerated in §3 (exact match) and returns 404 for the rest; a TCP request carrying any identity header gets 400. (§2, §3)
2. Architecture test: building identity from headers happens only on the UDS listener's path; rate limiting, the request log and the auth bypass never read identity headers. (§2)
3. Chart render test: the deployment has exactly one oauth2-proxy, and unauthenticated `/api/` requests get 401; only tenant-api and oauth2-proxy mount the human socket; the UDS always exists; oauth2-proxy and tenant-api mount the same configuration file; oauth2-proxy in a production deployment has no login-skipping condition that covers `/api/` (the development topology of §3 is contained per ADR-022 and is exempt). The nginx configuration the portal container actually loads (whether from the chart or the image) contains no forwarding. (§2, §5)
4. Every backend path called from the portal source is under the `/api/` prefix; the simulate path returns 501 with the original `code` and `error` text. (§5)
5. A scan of helm, k8s manifests and rule packs: no configuration relays identity headers over the network to any component; the chart keys deleted in §6 do not exist; threshold-govern's CronJob, its NetworkPolicy allowance and its alert do not exist. The environment variables deleted in §6 are not in tenant-api's known list. (§4, §5, §6)
6. tenant-api refuses to start when oauth2-proxy's configuration file does not have the shape in §2, with fixtures covering at least: keeping the client's value, a fixed value, a prefix, the wrong claim, missing email or groups, environment-variable references, and non-canonical field-name spellings; an `_rbac.yaml` with no rules denies all access, and no switch exists that restores open mode; a write by a non-platform-admin that changes a tenant's environment or domain tags, or overwrites a tenant file whose tags cannot be read, is rejected; this check and the write scope are made under the writer lock, on the tree the write lands on. (§7)
7. `tenant-api config check` lists untagged tenants and tenants whose tags cannot be read per scope axis and exits non-zero, tested against a fixed fixture and checked rule by rule against the server's tenant-list decision; CI runs it against the sample configuration in the repo. (§7)
8. Reference check: every reference written as `ADR-NNN §section` in the repo (and sections chained after it on the same line) must resolve to an existing section of that ADR (matched by the section heading's number or name); `ADR-027` followed by a section ID of the 2026-07 draft (`D2-B`, `LD-6 P4b`, `MED-8` and the like; the full list is in the check itself) always fails. (This ADR and [#1667](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1667))
9. CI runs oauth2-proxy's configuration check (`--config-test`) with the image pinned by the chart (the same digest) against the configuration file the chart generates, and uses the same version of the oauth2-proxy library to assert that routing matches the example in §10. (§10)

### 10. oauth2-proxy routing configuration

Tested against the oauth2-proxy version pinned by the chart (v7.15.5):

- **Legacy flags cannot do it**: the route path of `--upstream=unix://…` is fixed at `/`, so the API cannot be placed at `/api/`; the portal is at `/` as well, the two collide, and configuration validation fails (`multiple upstreams found with path "/"`).
- **The structured configuration file (`--alpha-config`) can**: each upstream's path and destination are set separately.

Configuration (excerpt):

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

With the structured configuration file, the provider, header injection and listener address live in the file, and the corresponding legacy flags can no longer be used alongside it (with them, oauth2-proxy fails to parse its flags and refuses to start); other flags such as session, cookie and `--api-route` (return 401 instead of redirecting when not logged in) stay as they are. The oauth2-proxy project marks the structure of this file as subject to change between minor versions; the file is parsed strictly, and oauth2-proxy refuses to start on a field it does not recognize, so a structural change fails immediately on upgrade instead of being silently ignored. §9 item 9 makes that failure happen in CI.

Fallback (not adopted, kept for the record): the Ingress routes `/api/` by path to tenant-api's oauth2-proxy and the portal is served without login. §7's claim-header injection needs the structured configuration file as well, so the fallback would not avoid it.

## Alternatives considered

- **Authenticate every hop, then enforce after an audit phase (the 2026-07 draft)**: see §Context. For a component relaying user identity, a token proves only the component; the set of pairings grows with the number of components.
- **Keep the external surface, just switch audit to enforce**: still trusts the user identity relayed by da-portal and recipe-preview, which shrinks the problem from "any Pod that can reach tenant-api" to "any Pod that can reach da-portal's internal port" without removing it.
- **da-portal forwarding the browser's session cookie to tenant-api's oauth2-proxy (two proxies sharing a session)**: still relays a human credential over the network; the cookie secret lives in two namespaces and cannot be checked mechanically for consistency; "who may log in" has two points of decision.
- **Ingress path routing with the portal served without login (as the primary design)**: an Ingress cannot point at a Service in another namespace, so one host needs two Ingresses merged by the controller; host, redirect URL and TLS must be paired by hand across charts. Kept as the fallback in §10.
- **The portal's nginx in front inside the pod, passing `/api/` to the same pod's oauth2-proxy (to avoid the structured configuration file)**: forwarding reappears in the portal container, which defeats §9 item 3; §7's claim headers still need the structured configuration file, so both costs are paid.
- **The chart generating both the oauth2-proxy configuration and tenant-api's claim-header setting from one set of values**: only Helm deployments stay mechanically consistent; the local-trial compose file would need a second hand-written copy.
- **A separate tenant-api flag declaring which headers oauth2-proxy overwrites**: the flag is itself another setting to keep in sync by hand, and tenant-api cannot detect when it is wrong.
- **tenant-api serving the portal's static files itself**: CSP, caching and SPA fallback would have to be rebuilt in Go, and the portal and tenant-api version lines would be tied together.
- **Fixing threshold-govern and building machine identity now**: see §4; it means building a whole identity plane for the only caller, which has never run. The trigger is in §4.
- **Keeping shadow mode for scope axes (off by default)**: would-deny counts only move with traffic and cannot show that nobody is affected; tenants can be enumerated, so a deterministic offline check is more reliable (Gatekeeper and Kyverno also rely on background scans for enumerable resources, using traffic-driven dry-run only for things that cannot be enumerated).
- **Using the reverse query as the pre-upgrade check**: it looks up one tenant at a time, needs an admin session, and does not evaluate the metadata axis.
- **Keep recipe-preview where it is and have it verify tenant-api via TokenReview**: it still needs a network port and one more set of verification settings; moving it into the pod removes that set.
- **oauth2-proxy verifying machine tokens via `--extra-jwt-issuers`**: local signature verification has no revocation; the audience of a ServiceAccount token is chosen by the requester, so the subject must still be matched; whether Pods can fetch the cluster issuer's JWKS depends on cluster configuration (unverified). Revisit when the §4 trigger fires.
- **A kube-rbac-proxy sidecar, or authorizing machines via SubjectAccessReview**: machine permissions would live in Kubernetes RBAC, a second source of authorization that the reverse query cannot see.
- **Relaying a user token (RFC 8693 token exchange, on-behalf-of)**: GitHub and GitLab CE access tokens are opaque, so there is no verifiable user token to relay; for OIDC customers that issue JWT access tokens, tenant-api can verify them directly (see the deferred item below).
- **Using the OIDC ID token as an API credential**: an ID token's audience is the OAuth client, not the API, and it can be replayed across services; the established guidance is that it should not be sent to APIs ([oauth.net](https://oauth.net/id-tokens-vs-access-tokens/)).
- **Verifying the user's JWT access token (the draft's D2-A)**: deferred. Trigger: a customer IdP that issues JWT access tokens with audience tenant-api and groups included ([RFC 9068](https://datatracker.ietf.org/doc/html/rfc9068)). It would be added as a new identity source on the UDS plane, without changing §1–§5.
- **Trusting a loopback source IP**: other containers in the same pod are loopback too; middleware that derives the source IP from headers can be spoofed.
- **HMAC-signed headers**: distributing and rotating the keys is on us, which amounts to building a weaker ServiceAccount token ourselves.
- **SPIFFE/SPIRE, service mesh**: running an entire identity plane for a handful of callers. Trigger: cross-cluster or non-Kubernetes machine callers, or a requirement for channel-bound identity.
- **Identity broker (Dex / Keycloak)**: Dex's access tokens are opaque; Keycloak can issue verifiable access tokens but needs a database and ongoing patching. Trigger: one deployment needs several login sources.
- **Provider prefixes on GitLab / GitHub groups**: no collisions with a single provider. Trigger: one deployment connects a second IdP.

## Consequences and known limitations

- **Breaking**:
  - Requests that still relay identity through da-portal or recipe-preview, or call the API over TCP, are rejected; the affected components must be upgraded together.
  - The login-protected portal is now served by the tenant-api deployment; the da-portal chart keeps only the static deployment.
  - threshold-govern's CronJob, write path and alert are removed, along with the corresponding documentation.
  - Federation tokens are issued through the portal; the public examples that call tenant-api directly with curl are rewritten (those examples never carried any credential in the first place).
  - An unconfigured or rule-less `_rbac.yaml` no longer allows reads.
  - Scope axes are always enforced: untagged tenants are invisible under scope-restricted rules; writes are constrained by environment and domain scope for the first time, so writes to tagged tenants outside the scope are rejected too; the organisation scope axis is new. List them with `tenant-api config check` and tag them before upgrading.
  - Only a platform admin restricted on no scope axis can change environment and domain tags, or overwrite a tenant file whose tags cannot be read, through the API; in a deployment whose admins are all restricted on environment or domain, tags and such broken files can be changed only through git.
  - The local trial environment (try-local) and tests that send identity headers over TCP are rewritten for the new topology.
- oauth2-proxy is the sole verifier of human identity: its version is pinned by digest and its security advisories are tracked.
- The structure of the structured configuration file may change between oauth2-proxy minor versions; upgrading oauth2-proxy may require updating the configuration file the chart generates, and the part tenant-api parses (§10).
- The GitHub provider supplies no additional claims, so scope in such deployments can be decided by group only (§7).
- tenant-api's pod carries the API, the portal and recipe-preview, which scale together.
- A user removed from a group keeps permissions until the session updates (§8).
- GHSA-3g2h-rf85-5rrv currently lists the L4 stopgap release as the patched version; once this ADR's release ships, the advisory's patched version and description need updating.

## Implementation

The file holding each check in §9. Paths starting with `cmd/` or `internal/` are relative to `components/tenant-api/`; the rest are relative to the repository root. A few clauses of §9 have no check yet; they are listed at the end.

1. TCP plane: `cmd/server/tcp_router_test.go` (`TestTCPRouter_RegistersOnlyTheProbeRoutes`, `TestTCPRouter_EverythingElseIs404`, `TestTCPRouter_IdentityHeaderIs400`).
2. Identity is built only on the UDS plane:
   - Identity is built from headers in one place, and identity header names appear only in their owners: `internal/rbac/identity_arch_test.go` (`TestHeaderResolutionOnlyInResolveIdentity`, `TestIdentityHeaderNamesOnlyInTheirOwners`).
   - That place reads headers only on requests from the UDS listener and ignores them on TCP: `internal/rbac/devbypass_test.go` (`TestResolveIdentity_IgnoresHeadersOnTCP`).
   - Rate limiting, the request log and the authentication bypass read no header: `internal/rbac/identity_arch_test.go` (`TestRateLimitLogAndBypassReadNoRequestHeader`).
3. Chart render:
   - One oauth2-proxy in the whole deployment: `tests/helm/test_identity_single_hop.py` (`test_the_only_oauth2_proxy_is_in_the_tenant_api_pod`, `test_no_raw_manifest_runs_an_oauth2_proxy`).
   - oauth2-proxy sets `--api-route`, covering exactly the paths §10's example sends to tenant-api, and its arguments carry no flag that skips login: `tests/oauth2-proxy/chart_test.go` (`TestChartConfig`).
   - The human socket always exists: `tests/helm/test_human_socket_guard.py` (`test_render_without_proxy_fails`, `test_socket_path_is_a_chart_constant`, `test_deployment_wires_the_socket`, `test_render_default_points_upstream_at_socket`); tenant-api does not start without the socket or config file path: `cmd/server/tcp_router_test.go` (`TestCheckIdentityPlaneFlags`). The portal container does not mount it: `tests/helm/test_tenant_api_portal_container.py` (`test_portal_container_mounts_no_identity_or_secret`).
   - oauth2-proxy and tenant-api mount the same config file: `tests/helm/test_oauth2_proxy_alpha_config.py` (`test_both_containers_read_the_same_file`).
   - The portal container's nginx config forwards nothing: `tests/helm/test_tenant_api_portal_container.py` (`test_portal_runs_the_charts_config_and_it_forwards_nothing`).
4. Every portal backend path is under `/api/`: the portal reaches the backend only through `apiFetch` in `tools/portal/src/interactive/tools/_common/api/api-fetch.js`, which refuses, before sending, a path that does not resolve to this origin under `/api/` (`tools/portal/tests/api-fetch.test.ts`); no other source file names `fetch`, `XMLHttpRequest`, `EventSource`, `WebSocket` or `sendBeacon` (`tools/portal/tests/network-calls-only-in-api-fetch.test.ts`, parsed with the TypeScript parser); simulate answers 501: `cmd/server/simulate_route_test.go` (`TestSimulateNotProvided`), with the `code` and `error` text the portal's nginx used to answer, word for word (tenant-api may add `request_id`): `tests/shared/test_portal_simulate_not_provided.py` (`test_tenant_api_answers_the_same_code`, `test_tenant_api_answers_the_same_body`).
5. No identity relay, and the removed settings are gone:
   - No configuration relays identity headers over the network: `tests/helm/test_identity_single_hop.py` (`test_no_rendered_object_but_the_oauth2_proxy_config_names_an_identity_header`, `test_no_raw_manifest_or_rule_pack_names_an_identity_header`).
   - Removed chart keys: `tests/helm/test_tenant_api_machine_identity_removed.py` (machine identity, the human socket and the relay allowances), `tests/helm/test_tenant_api_scope_switches_removed.py` (the scope-axis and open-mode switches).
   - threshold-govern's CronJob, NetworkPolicy allowance and alert: `tests/shared/test_threshold_govern_write_path_removed.py`.
   - Removed environment variables are not in the known list, and setting one stops the start: `cmd/server/env_check_test.go` (`TestCheckTAEnv_RefusesSettingsADR027Removed`).
6. Authorization scope:
   - An oauth2-proxy config file of the wrong shape is refused: `internal/proxyconfig/proxyconfig_test.go` (`TestLoad_RefusesAmbiguousShapes`, `TestParse_Refusals`), fixtures in `internal/proxyconfig/testdata/`.
   - An `_rbac.yaml` with no rules denies all access: `internal/rbac/empty_config_mode_test.go`; no switch restores open mode: `tests/helm/test_tenant_api_scope_switches_removed.py` and `TestCheckTAEnv_RefusesSettingsADR027Removed` above.
   - Tag changes and overwriting a file whose tags cannot be read, judged under the writer lock: `internal/handler/label_guard_test.go`.
7. `tenant-api config check --kind scope`: `cmd/server/scope_check_test.go`; agreement with the server (the environment and domain axes): `internal/handler/scope_gap_planes_test.go` (`TestScopeGapsMatchEveryPlane`); run in CI on the sample config: the `Scope check on the try-local sample config (ADR-027 §7)` step of `.github/workflows/ci.yml`.
8. ADR reference check: `scripts/tools/lint/check_adr_section_refs.py` (pre-commit and CI), tested in `tests/lint/test_check_adr_section_refs.py`.
9. oauth2-proxy config check: CI job `go-tests-oauth2-proxy`. Library side: `tests/oauth2-proxy/chart_test.go` (`TestChartConfig`, which loads the config file the chart generates with the pinned library version and asserts the routing against §10's table), `tests/oauth2-proxy/routing_test.go` (`TestADRSection10Routing`, §10's example itself) and `tests/oauth2-proxy/version_test.go`; image side: `tests/oauth2-proxy/image_config_test.sh` (runs `--config-test` on the file `TestChartConfig` writes).

Clauses with no check yet:

- Item 3, "`/api/` answers 401 without a session": only the `--api-route` setting and the paths it covers are checked; no unauthenticated request is sent to oauth2-proxy.
- Item 3, "nothing skips login", is checked on the arguments only, not on environment variables; the chart has no setting through which a user could inject oauth2-proxy arguments or environment variables.
- Item 7's agreement check covers the environment and domain axes; the organization axis is not compared.
- The start-up wiring that stops on a config file of the wrong shape (`cmd/server/main.go` exits when parsing fails) has no test; the parsing itself is covered by item 6.

## Related

- [#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040): implementation and release tracking
- [#1667](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1667): the ADR-027 file does not exist
- [#962](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/962): Identity & Routing epic
- [GHSA-3g2h-rf85-5rrv](https://github.com/vencil/Dynamic-Alerting-Integrations/security/advisories/GHSA-3g2h-rf85-5rrv)
- [ADR-009](./009-tenant-manager-crud-api.md): the original decision to delegate authentication to oauth2-proxy
- [ADR-022](./022-dev-auth-bypass-four-layer-containment.md): containment of the development-mode auth bypass
- [ADR-036](./036-single-parser-effective-config.md): precedent for providing computation over a pod-internal UDS
- [`governance-security.md`](../governance-security.en.md): the L4 stopgap and the L7 root cause
