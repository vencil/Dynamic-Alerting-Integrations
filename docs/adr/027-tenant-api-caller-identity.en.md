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

🟡 **Proposed** (rewritten 2026-11-04; on the same day the owner settled the three formerly open items: the browser entry point, threshold-govern, and shadow mode for scope axes). Moves to Accepted once the owner approves the full text. §10 lists premises that must be verified before implementation.

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
6. It takes effect in one breaking release, with no soak period and no switches (§6).
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
- TCP listener: no identity; it only serves the endpoints in §3. A TCP request carrying any identity header (`X-Forwarded-Email`, `X-Forwarded-Groups`, configured claim headers) gets 400, so an old configuration still relaying identity fails immediately instead of being silently ignored.
- Which listener a connection came from is stamped by the server when the connection is set up; a request cannot rewrite it.
- Everything that needs the caller's identity (including rate limiting and the request log) reads the identity object, never the headers directly.
- The development-mode auth bypass injects the identity object directly, no longer headers.
- The UDS is no longer optional: tenant-api refuses to start without it. Within the pod only the tenant-api and oauth2-proxy containers may mount the socket; oauth2-proxy must strip identity headers sent by the client before setting its own.

**Trade-off**: one more mandatory socket and shared volume; in exchange, where human identity may be asserted becomes a topology fact rather than a setting.

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
- The "simulate not provided" 501 that da-portal's nginx used to return is now returned by tenant-api, with the same status code and body.

Consequent changes:

- **da-portal**: no longer has its own oauth2-proxy, Ingress or reverse proxy. The da-portal chart keeps only the static, no-login deployment (Tier-1).
- **recipe-preview**: moves into tenant-api's pod and provides pure computation only over a pod-internal UDS, like the generator in [ADR-036](./036-single-parser-effective-config.md). The browser calls tenant-api, which authorizes the caller and then calls recipe-preview; recipe-preview neither receives nor relays any identity, and has no network port any more.
- The concept of a "relay" is removed entirely: no relay list, and no "verified relayer" identity class.
- The deployment ends up with one Ingress, one redirect URL and one session cookie; there are no login settings left to keep in sync by hand across two charts.

**Trade-off**: tenant-api's pod gains two containers that share its resources, and the portal image version is pinned in the tenant-api chart; in exchange, user identity only ever crosses the single oauth2-proxy → UDS hop, and "who may log in" has one point of decision.

### 6. One-shot cutover, no soak

- The changes in this ADR take effect in a single breaking release, shipping all affected components together. The release notes show what the old configurations that get rejected after upgrading look like.
- All switches about auditing, relaying, "whether the UDS is enabled", "whether oauth2-proxy is enabled", scope-axis shadow mode and open mode are deleted. A render test asserts the chart keys do not exist; tenant-api's environment variables are removed from its known list, so an old configuration still setting them makes tenant-api refuse to start through the existing "unknown `TA_*` variable" check (§9).
- Rationale: very few adopters; audit and shadow phases cannot show the absence of forgery or that nobody is affected; and none of these switches is in any released image, so the soak never actually started.

### 7. Authorization scope model

Roles come from IdP groups; scope (which tenants, environments, domains, organisations) comes from verified identity attributes.

- **Which attributes may be used for authorization**: only claims that oauth2-proxy extracted from a signed token and that the user cannot change. Claim headers are read only on the UDS (§2). tenant-api accepts a claim-header configuration only when the chart explicitly declares that oauth2-proxy overwrites those headers; without that declaration it refuses to start.
- **Values carry a source namespace**, so same-named values from different sources cannot impersonate each other.
- **Scope-determining data is platform-controlled**: the organisation-to-tenant mapping lives in a configuration layer only platform admins can change. Environment and domain tags currently live in each tenant file's `_metadata` and can be changed through the write API; from now on only platform admins may change them, and an ordinary write that changes them is rejected, so someone with write access cannot widen their own scope by retagging.
- **Always enforced, no shadow mode**: read scope, write scope and organisation scope are each judged independently, with no "record but don't block" option. Untagged tenants are invisible and unwritable under rules that carry a scope restriction.
- **An offline check lists affected tenants before upgrading**: `tenant-api config check` (which today only checks that a single file parses) is extended to read `_rbac.yaml`, the organisation mapping and the tenant configuration tree together, and lists, for each scope axis, "scope-restricted rules × untagged tenants" and exits non-zero if it finds any. It reads only configuration, so its result is fixed and independent of traffic; run it with the new image before upgrading, and in CI.
- **No open mode**: when `_rbac.yaml` is not configured, or contains no rules, all access is denied; the "authenticated means readable" mode and the switch that restores it are deleted.
- The reverse query stays: given a tenant, list the rules that can touch it, for audit.

GitLab groups are matched by full path (`org/subteam`) as-is, without a provider prefix; a prefix is introduced when one deployment connects a second IdP (trigger in §Alternatives considered).

### 8. Defense in depth that remains

- NetworkPolicy stays, but is no longer the only control; the safety of this ADR does not depend on whether the CNI enforces it.
- oauth2-proxy fetches groups at login and keeps them in the session; after a user is removed from a group, permissions update only when the session is refreshed or expires (providers differ on whether a refresh re-fetches groups), mitigated by a shorter session lifetime.

### 9. Decisions expressed as tests and lints

This document is explanation; the checks below are the single source of truth for these decisions. Once implemented, §Implementation lists the file for each check.

1. TCP registers only the paths enumerated in §3 (exact match) and returns 404 for the rest; a TCP request carrying any identity header gets 400. (§2, §3)
2. Architecture test: building identity from headers happens only on the UDS listener's path; rate limiting, the request log and the auth bypass never read identity headers. (§2)
3. Chart render test: the deployment has exactly one oauth2-proxy, and unauthenticated `/api/` requests get 401; only tenant-api and oauth2-proxy mount the human socket; oauth2-proxy has no option set that disables header stripping; the UDS always exists. The nginx configuration the portal container actually loads (whether from the chart or the image) contains no forwarding. (§2, §5)
4. Every backend path called from the portal source is under the `/api/` prefix; the simulate path returns 501 with the original body. (§5)
5. A scan of helm, k8s manifests and rule packs: no configuration relays identity headers over the network to any component; the chart keys deleted in §6 do not exist; threshold-govern's CronJob, its NetworkPolicy allowance and its alert do not exist. The environment variables deleted in §6 are not in tenant-api's known list. (§4, §5, §6)
6. Configuring claim headers without declaring that oauth2-proxy overwrites them stops startup; an `_rbac.yaml` with no rules denies all access, and no switch exists that restores open mode; a write by a non-platform-admin that changes a tenant's environment or domain tags is rejected. (§7)
7. `tenant-api config check` lists untagged tenants per scope axis and exits non-zero, tested against a fixed fixture; CI runs it against the sample configuration in the repo. (§7)
8. Reference check: every section reference after an `ADR-NNN` in the repo must resolve to an existing section of that ADR (matched by the section heading's number or name); `ADR-027` followed by a legacy ID from §Legacy references always fails. (This ADR and [#1667](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1667))

### 10. Premises to verify before implementation

1. **Can oauth2-proxy route by path to a UDS**: §5 needs the oauth2-proxy version pinned by the chart to support several upstreams, with the API-path one being `unix://`. Test it with a minimal configuration first. The portal upstream may be pod-internal loopback, since the portal does not read identity headers.
2. **Fallback if it cannot**: the Ingress routes `/api/` by path to tenant-api's oauth2-proxy and the portal is served without login; §5's requirements on 401 and redirecting to login apply equally.

## Alternatives considered

- **Authenticate every hop, then enforce after an audit phase (the 2026-07 draft)**: see §Context. For a component relaying user identity, a token proves only the component; the set of pairings grows with the number of components.
- **Keep the external surface, just switch audit to enforce**: still trusts the user identity relayed by da-portal and recipe-preview, which shrinks the problem from "any Pod that can reach tenant-api" to "any Pod that can reach da-portal's internal port" without removing it.
- **da-portal forwarding the browser's session cookie to tenant-api's oauth2-proxy (two proxies sharing a session)**: still relays a human credential over the network; the cookie secret lives in two namespaces and cannot be checked mechanically for consistency; "who may log in" has two points of decision.
- **Ingress path routing with the portal served without login (as the primary design)**: an Ingress cannot point at a Service in another namespace, so one host needs two Ingresses merged by the controller; host, redirect URL and TLS must be paired by hand across charts. Kept as the fallback in §10.
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
  - The local trial environment (try-local) and tests that send identity headers over TCP are rewritten for the new topology.
- oauth2-proxy is the sole verifier of human identity: its version is pinned by digest and its security advisories are tracked.
- tenant-api's pod carries the API, the portal and recipe-preview, which scale together.
- A user removed from a group keeps permissions until the session updates (§8).
- GHSA-3g2h-rf85-5rrv currently lists the L4 stopgap release as the patched version; once this ADR's release ships, the advisory's patched version and description need updating.

## Legacy references

Existing code cites this ADR with the numbering of the 2026-07 draft. Those references are rewritten when the reference check in §9 item 8 lands, and this table is then deleted; until then, map them as follows:

| Legacy reference | This ADR |
|---|---|
| `D1`, `G4`, and machine-identity audit references tagged with implementation PR numbers | §4 (machine identity deferred) |
| `D2-B`, `O1`, `L7` | §1, §2, §5 (da-portal relay removed) |
| `Phase-1` | §6 (no soak) |
| `§2.3`, `§2.5` | §2 |
| `MED-7` | §3 |
| `MED-8`, `D4`, `LD-6 P1`–`P7d` | §7 |
| `D2-A` | §Alternatives considered (deferred) |

## Implementation

Not implemented yet. Once the implementation PRs merge, the file for each check in §9 is listed here.

## Related

- [#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040): implementation and release tracking
- [#1667](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1667): the ADR-027 file does not exist
- [#962](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/962): Identity & Routing epic
- [GHSA-3g2h-rf85-5rrv](https://github.com/vencil/Dynamic-Alerting-Integrations/security/advisories/GHSA-3g2h-rf85-5rrv)
- [ADR-009](./009-tenant-manager-crud-api.md): the original decision to delegate authentication to oauth2-proxy
- [ADR-022](./022-dev-auth-bypass-four-layer-containment.md): containment of the development-mode auth bypass
- [ADR-036](./036-single-parser-effective-config.md): precedent for providing computation over a pod-internal UDS
- [`governance-security.md`](../governance-security.en.md): the L4 stopgap and the L7 root cause
