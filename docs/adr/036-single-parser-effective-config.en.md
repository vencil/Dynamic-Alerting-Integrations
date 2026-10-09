---
title: "ADR-036: The Generator Parses conf.d Routing and Policy Once; Go Readers Consume Its Output"
tags: [adr, config, routing, domain-policy, tenant-api, security]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: en
---

# ADR-036: The Generator Parses conf.d Routing and Policy Once; Go Readers Consume Its Output

> **Language / 語言：** [中文](./036-single-parser-effective-config.md) | **English (Current)**

## Status

🟡 **Proposed** (drafted 2026-10-09).

- The direction (D3) and its open questions were settled by the owner in [#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766) and hub [#2486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2486).
- The design proposal went through one round of external adversarial review by two different models, one engineering-focused and one focused on parser differentials and security. Every point they broke has been reworked into this document.
- The ADR text itself went through another external review by a different model. The verified points are merged, including the re-serialisation done by batch writes (the owner decided the merge moves to the sidecar).
- Awaiting the owner's approval.

## Summary

**Problem**: the same conf.d is read twice, by two YAML parsers: the Python route generator (pure-Python PyYAML; the authority) and the Go readers da-guard and tenant-api (vendored yaml.v3). Go's verdict must equal the generator's, so Go grew a large hand-written PyYAML emulation. That path does not converge: [#2763](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/2763) took four fix rounds and each introduced a new regression, and main still has shapes that Go reads more loosely than the generator.

**Decisions**:

1. Each semantic has exactly one parser. **Routing, domain policy and tenant enumeration belong to the generator**; thresholds belong to the exporter.
2. The tenant-api pod gains a da-tools sidecar in which **the real generator code** emits an effective-config JSON and serves a write-validation endpoint. tenant-api no longer parses routing or policy YAML itself.
3. Write validation is **absolute**: the generator runs on the candidate tree, the result is bound to `(path, sha256, base_rev)`, and the writer checks that binding under its lock before committing.
4. During the transition the write verdict is the **union** of Go's and the generator's (either refusing refuses); when the sidecar is unavailable, writes get 503. Once the trigger is met, the Go emulation is deleted.

**Acceptance condition (non-negotiable)**: no reader may be looser than the generator. It may neither read a policy the generator drops nor miss one the generator enforces. From P1 this holds for writes and reads; in P0 it holds for writes only (see "Transition"). How it is enforced is in "Mechanising the acceptance condition".

## Problem

### Why emulating PyYAML in Go does not converge

- Every fix round hand-wrote another PyYAML construction path in Go: `construct_mapping` skipping null keys, `construct_yaml_omap`, `raw_text_sequences`, `flatten_mapping`. Each one patched exposed the next, and the construction order itself decides PyYAML's verdict.
- The alternatives tried earlier failed for the same reason: judging on yaml.v3 nodes whether PyYAML would accept, having two parsers each decide a language subset, byte rules against directives and TABs, rewriting directive literals.
- Differential fuzzing finds divergences but cannot prove equivalence.

### Looser divergences measured on main (`d8525326`, 2026-10-09)

| Shape | Generator | da-guard | tenant-api |
|---|---|---|---|
| 600 levels of nesting | drops the file | reads the policy | reads the policy |
| `!!set` on `domain_policies` / a domain / `constraints` | does not enforce | reads it | reads it |
| a mapping with a collection key inside a merge value | drops the file | refuses | reads the policy |
| a UTF-8 BOM straddling byte 512, inside a comment | enforces the policy | silently reads no policy | silently reads no policy |
| `!!merge q:` in a tenant's `_routing` ([#2700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2700)) | the merged receiver violates the policy, rc 1 | a plain key, rc 0 | [unverified] |
| an overridden merge source with an unbuildable value in a tenant's `_routing` ([#2713](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2713)) | refuses the file, rc 1 | rc 0 | [unverified] |

The full list, including the stricter divergences, is in [#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759).

### Not just the policy file

When tenant-api judges rules such as `require_critical_escalation`, it uses the tenant's merged routing (`_routing_defaults`, `routing_profiles`, the tenant's `_routing`), and Go parses all of those layers too. Tenant enumeration, duplicate-tenant and level detection (the exporter's `ScanDirTree` and `declaredTenantIDs`) are likewise computed by both parsers.

## Decisions

### 1. Owners of each semantic

| Semantic | The one parser | How other readers get it |
|---|---|---|
| Routing (`_routing_defaults`, `routing_profiles`, a tenant's `_routing`), domain policy, tenant enumeration and directory mapping | the route generator (Python) | read the effective-config JSON the generator emits |
| Thresholds, served values | the exporter (Go) | already so: Python tools read them as JSON from `da-guard served-values` / `effective` (`_lib_tenant_values`) |

The thresholds row is the existing precedent in the other direction; this ADR does not change it.

### 2. Architecture

- The tenant-api pod gains a **da-tools sidecar** running the existing da-tools image, which contains the generator.
- It talks to tenant-api over a **Unix domain socket** in a shared emptyDir. No TCP port is opened: only processes that have that volume mounted and pass the socket file's permissions can connect; containers in the pod without the mount, and network peers, cannot reach it.
- How the sidecar reads the tree: for a given commit, it **materialises** the tree into a private directory with `git ls-tree -r` plus `git cat-file --batch`.
  - It does not read the shared working tree: PR mode switches branches in place in `/conf.d` with `git checkout -f`.
  - It does not use `git archive`, which applies `.gitattributes` such as `export-ignore`. This repo has no such marker, but if a customer's conf.d repo marked `_domain_policy.yaml` `export-ignore`, the export would silently drop it, and the sidecar would read more loosely than the customer's CI.
- The materialised tree is cached per `base_rev`; parse results are cached per file, keyed by content hash plus generator version. Results derived across files are not cached.
- Sidecar safeguards: limits on request size, nesting depth and run time; a memory limit of 128–256Mi; a read-only root filesystem with tmpfs scratch.

### 3. The effective-config JSON (for reads)

- Contents: whether each policy file is usable; per domain its tenants, forbidden / allowed receiver types and `require_critical_escalation`; each tenant's merged routing; the tenant-to-file and tenant-to-directory mapping.
- Envelope: `schema` (a major mismatch makes the whole document unusable), `min_reader`, `generator_version`, `tree_rev`.
- Encoding follows the I-JSON (RFC 7493) constraints:
  - Python: `ensure_ascii=False, allow_nan=False, sort_keys=True`, then UTF-8 encoding. A lone surrogate fails at encoding time, and that generation fails.
  - Not `ensure_ascii=True`: Go's `encoding/json` silently turns a `\ud800` escape into U+FFFD, which would move the divergence into Go.
  - Go decodes strictly: duplicate keys (a token-stream check, no new dependency), invalid UTF-8 and unknown fields are refused; numbers use `json.Number`, and durations and thresholds are always emitted as strings.
  - Written atomically (temp file plus rename).
- A constraint kind the reader does not know makes that policy unusable; it must never be ignored (as with X.509 critical extensions).
- **When it is regenerated**: tenant-api notifies the sidecar after every commit, and each poll cycle compares HEAD and regenerates on change.
- Last-good **serves reads only**, and:
  - It is kept per file, like today's policy watcher. Only files that still exist at the new `tree_rev` are kept; a policy file deleted in the new tree must not live on as last-good.
  - It carries `tree_rev` and its generation time; past an age limit since the last successful generation it becomes unusable and alerts.

### 4. Write validation

- **The criterion is absolute**, as today's `judgePutBody` is. The sidecar runs the generator on the candidate tree (the base tree with this write's file replaced) and refuses if either:
  - any blocking finding's subject is the tenant or file being written; or
  - the candidate tree has a tree-level failure the base does not have (an unreadable file, a duplicate tenant, a routing-tree error, and so on).
- Finding strings are not diffed: a tenant already in violation would be let through when its rewrite reproduces the same line, and a parse failure prints `FAIL:`, not `ERROR:`, so a prefix match misses it entirely.
- **Prerequisite**: the generator first emits structured findings (`policy`, `tenant`, `file`, `kind`, blocking or not), so that the criterion above does not depend on strings.
- **Tree-level failures are compared by structured `kind` plus `file` too**, never by text. "Not in the base, present in the candidate" is a set comparison on those fields; findings about the written tenant are judged absolutely. Neither replaces the other.
- **When the base itself already has a tree-level failure**: a write is allowed when its subject is clean and it adds no tree-level failure, so one broken neighbour file cannot block everyone.
- **What gets validated is decided by "does this produce new file bytes"**, not by Go's reading of file contents. Every write that rewrites file bytes goes to the sidecar; the fail-open in `tenantBlockOnDisk` (an unreadable file read as empty, the patch judged alone) is removed.
- **Binding and races**:
  - The sidecar returns the `(path, sha256(bytes), base_rev)` it judged; the writer checks all three under its lock and revalidates on any mismatch.
  - What is validated is **the exact bytes to be written**, and the writer writes byte-identical content. A PUT body is already the source text.
  - **Batch-patch merging moves to the sidecar** (owner decision, 2026-10-09). Today `mergePatchYAML` reads the existing file with yaml.v3 and re-emits it, which writes Go's reading into the file (for example `!!merge q:` rewritten as a plain key `q:`) even when the op touches no routing. The sidecar instead merges with the generator's reading and returns the resulting bytes; tenant-api only writes them. During P0, Go still merges and the sidecar validates the merged bytes (this stops violating results but not a silent, non-violating change of meaning); from P1 the sidecar merges.
  - In PR mode, validation runs inside `WritePRChecked`'s fresh-base closure.
  - Direct mode does not push, so `base_rev` is the local HEAD; under the lock, HEAD must still equal `base_rev`.
- **Batch writes**: one request carries the whole batch; both the final state and every prefix state are validated, and the lock is held until every commit is done. The number of ops per batch is capped, and prefix validation shares one materialised tree and parse cache.
- **Any failure refuses**: an exception, a non-zero rc, truncated output, or output without an explicit OK marker plus the echoed hash are all refusals.
- **Errors returned to API clients** contain only findings whose subject is the tenant being written, never another tenant's violations.
- In P0 the Go verdict is one half of the union (see "Transition"). From P1 no Go pre-check is kept, so a yaml.v3 parse failure cannot refuse a write the customer's CI accepts.

### 5. Version alignment

- The invariant that can be guaranteed is "**tenant-api is no looser than the generator inside the sidecar**". "Equal to the version the customer's CI uses" cannot be guaranteed: the customer's da-tools version is independent of tenant-api.
- How version skew is narrowed:
  - The tenant-api chart pins a da-tools image by default, and CI runs a contract test on that pair.
  - The configuration `da-tools init` generates pins a tag instead of `latest`.
  - New: a policy file may declare `generator_min_version` (no such key exists in the repo today); the sidecar refuses writes when it is older. Only the generator reads this key; Go does not parse the policy file for it.
  - tenant-api exposes `generator_version` as a metric.
  - A test requires every constraint kind the generator can emit to be in Go's known list.

### 6. Transition

| Phase | Write verdict | Sidecar unavailable | Reads |
|---|---|---|---|
| P0 | the **union** of Go and the generator: either refusing refuses | writes that touch routing or policy get 503; no fallback to Go alone | the existing Go reading, with disagreements recorded |
| P1 | the generator only; the Go emulation (`shapeWork` and so on) deleted | writes get 503 | read the effective-config JSON |

- P0 does not fall back to Go alone: whether the sidecar is available can be influenced by requesters (floods of expensive requests), and Go alone has known looser divergences, so a fallback would let an attacker pick the looser judge. For comparison: Kyverno defaults to `failurePolicy: Fail`; Gatekeeper defaults to Ignore, which risks bypass when it is used as a security control.
- The P0 → P1 trigger: over a shadow period, every disagreement carries a stable reason code and is on an explicit "explained" list, with no unexplained disagreement. The length of the shadow period is open.
- A rollback flag `--policy-source=go|union|generator`. After P0, `go` is not the default and choosing it is logged loudly.
- The existing `--policy-unavailable-open` keeps its meaning (let writes through when the policy file is unusable) and does **not** cover an unavailable sidecar; there is no open switch for that.
- **Reads during P0 are an explicit exception to the acceptance condition**: GET and the policy watcher keep the Go reading, so the known divergences listed in #2759 persist on the read side until P1. Writes must meet the acceptance condition already in P0.

### 7. da-guard

da-guard ships in the same da-tools image as the generator. It obtains routing and policy verdicts from the generator instead of parsing that YAML itself; threshold reads (exporter semantics) are unchanged. The exact interface is an open question.

## Mechanising the acceptance condition

- **Fuzzing**: generate random conf.d trees → run the generator to get the effective-config JSON and structured findings → give the same tree to the Go readers → assert in both directions that the policies and tenants Go enforces **equal** the JSON's (reading an extra one and missing one both fail), and that decoding the JSON either fully succeeds or refuses the whole document. The comparator itself does not parse YAML.
  - From P1, this equality must hold for writes and reads.
  - In P0 only the write side is asserted: the union verdict refuses every write the generator refuses. The read side's known divergences are the explicit exception listed under "Transition" and are not asserted until P1.
- **Frozen regression corpus**: the shapes from #2759, #2700, #2713 and #2674, together with the existing `merge_key_policy_corpus.json`, as fixed cases.
- It runs in CI on the image pair the chart pins, not ad hoc.

## Cost (measured, 2026-10-09)

| Item | Measurement |
|---|---|
| Generator end to end, 1000 tenants | 1.72 s, 38 MB; 1.06 s of that is parsing, and every file is parsed twice |
| One file with SafeLoader | 0.47 ms |
| Python cold start | 0.11 s, 25 MB |
| `ls-tree` + `cat-file` materialising 1000 files | 0.21 s, 12 MB |
| Image size (compressed, amd64) | da-tools v2.9.0 36.9 MB; tenant-api v2.7.0 14.0 MB |

With a warm cache, a single write costs mainly one file's parse plus the checks. Checks plus rendering take about 0.6 s; that is an estimate, not measured on its own.

## Industry practice

- Parse once, consume a canonical form downstream: Kubernetes' `sigs.k8s.io/yaml` converts to JSON first; Envoy converts to protobuf; OPA / conftest judge `terraform show -json`, not HCL source.
- The parsing tool runs as a sidecar in the consumer's pod: Argo CD's Config Management Plugins (sidecars of the repo-server), OPA's sidecar deployment, Envoy Gateway reaching an OPA sidecar over a Unix socket.
- Interchange-format constraints: I-JSON (RFC 7493).
- LangSec: recognise the input fully before processing it.

## Rejected alternatives

| Direction | Why rejected |
|---|---|
| Keep emulating PyYAML in Go | does not converge, see "Problem" |
| D1: a policy lock file committed to git | covers policy only, routing is still parsed by Go; a forgotten regeneration means 503 on cold start and enforcing the old version when warm |
| D2: a custom grammar shared by all three | YAML 1.1 scalar types collide with real tenant ids (`no`, `0777`); a breaking change |
| O2: the generator switches to libyaml | aligns only the scanner layer; the regressions are all in the constructor layer |
| A faithful Go port of PyYAML (about 4–5k lines) | fallback, considered only if the sidecar's cost is rejected |
| A JSON-only write API | a breaking change; Go would still emit YAML for PyYAML to read, the same double parser in the other direction |
| Diffing finding strings | looser than today's absolute verdict, see "Write validation" |
| Exporting the tree with `git archive` | applies `export-ignore`, which can drop the policy file |
| Falling back to Go alone when the sidecar is down in P0 | lets an attacker pick the looser judge |
| Using last-good for writes | under version skew an old policy is kept indefinitely |
| `ensure_ascii=True` | moves the divergence into Go's `encoding/json` |

## Open questions

1. **da-guard's interface**: does da-guard call the generator's Python module directly, or read a JSON the generator produced first (for example `--effective-json`)? The latter keeps da-guard usable where there is no Python (a binary built in CI).
2. **The length of the P0 shadow period** and how the "explained disagreements" list is reviewed.
3. **tenant-api's GET endpoints**: the threshold parts are exporter semantics and out of scope here; the routing parts move to the JSON on P1's schedule.
4. **P1 deletion scope**: of the three vendored yaml.v3 patches, whether those serving only the policy path (`SpacesOnly`, nonspecific-tag) are removed too, and whether `uniquekeys` still has exporter users.

**Trade-offs decided and recorded here**:
- The authorisation boundary of the Unix socket is "processes that have the volume mounted and pass the socket file's permissions", meaning the processes in the tenant-api and sidecar containers can call the validation endpoint, which is accepted. A container without the mount cannot.
- The sidecar reads git objects by rev with `cat-file` / `ls-tree` only and touches neither the index nor the working tree, so it does not contend with PR mode's `checkout -f` for `index.lock`.

## Implementation plan

1. The generator emits structured findings (prerequisite).
2. Sidecar: the effective-config JSON and the write-validation endpoint; the Helm chart gains the container, socket and resource settings.
3. tenant-api: write paths call the sidecar (P0 union); reads move to the JSON.
4. da-guard obtains routing and policy verdicts from the generator.
5. P1: delete the Go PyYAML emulation; re-test [#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759), [#2700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2700), [#2713](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2713) and [#2674](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2674) on main, then close them.

## Related

- [#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766) (design and external review record), hub [#2486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2486)
- [ADR-023 tenant-api write plane: the single-writer invariant](./023-write-plane-single-writer-invariant.md) (Chinese only)
