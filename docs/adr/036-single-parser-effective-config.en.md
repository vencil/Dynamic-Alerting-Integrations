---
title: "ADR-036: Routing and Domain-Policy Config Is Parsed Once, by the Generator"
tags: [adr, config, routing, domain-policy, tenant-api, security]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: en
---

# ADR-036: Routing and Domain-Policy Config Is Parsed Once, by the Generator

> **Language / 語言：** [中文](./036-single-parser-effective-config.md) | **English (Current)**

## Status

✅ **Accepted** (drafted 2026-10-09, approved by the owner 2026-10-09).

- The decisions were settled by the owner in [#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766) and [#2486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2486).
- The design and this document each went through one round of external adversarial review (by a different model); the verified points are merged.
- The owner has approved this document.

## Summary

**Problem**: the same conf.d is read twice: once by the route generator (Python), and again by da-guard and tenant-api (both Go; together called "the Go side" below) with a different YAML parser. The two readings differ, and the Go side sometimes lets through config the generator would block. The Go side has tried to line up by hand-writing code that imitates how Python reads YAML; several rounds of fixes have not closed the gap.

**Decisions**:

1. Routing, domain policy and the tenant list are parsed only by **the generator**; thresholds are still parsed by the exporter.
2. The tenant-api pod gains a container that runs the real generator. It outputs the resolved config (JSON) and validates every write. tenant-api no longer parses this YAML itself.
3. The switch happens in two phases:
   - Phase 1: old and new run side by side, and a write is refused if either side refuses it.
   - Once every difference between the two is understood, the Go side is nowhere looser than the generator, and side-by-side running has lasted at least 14 days with real comparison volume, phase 2 starts and the Go imitation code is deleted. The full conditions are under "When phase 2 starts".
4. da-guard stops judging routing and policy; the generator's `--validate --strict` judges them.

**Impact on users and operators**:
- tenant-api gains a container (the da-tools image, about 37 MB compressed).
- When that container is unavailable, writes that touch routing or policy get 503; they are not let through.
- A CI that runs only da-guard no longer covers routing and policy, and needs an extra `da-tools generate-routes --validate --strict` step. This is a breaking change.

**Non-negotiable acceptance condition**: no reader may be looser than the generator. It must neither read a policy the generator drops nor miss one the generator enforces.
- From phase 2, this holds for writes and reads.
- In phase 1 it holds for writes only; reads keep known gaps until phase 2 (see "Two-phase switch").

## Problem

### An example

YAML has a way to "merge another mapping in", usually written `<<:`. The Python parser the generator uses (PyYAML) treats any key tagged `!!merge` as such a merge; the Go side's parser recognises only a literal `<<`. This tenant file falls into that gap:

```yaml
# t1.yaml
tenants:
  t1:
    _routing:
      !!merge q: {receiver: {type: slack, api_url: "https://hooks.slack.com/x"}}
```

- **The generator** treats it as a merge, so t1's receiver is `slack`. If a domain policy forbids slack, the generator reports an error and exits 1.
- **da-guard** treats `q` as an ordinary key and takes the receiver from the platform default. It judges the config compliant and exits 0.

The same file gets opposite verdicts, and it is the Go side that lets it through.

### Not just one example

Measured on main on 2026-10-09:

| Shape | Generator | Go readers |
|---|---|---|
| a `!!merge`-tagged key in a tenant's `_routing` | the merged receiver violates the policy; blocked | an ordinary key; let through |
| a policy file nested 600 levels deep | the whole file unusable | reads and enforces the policy |
| a UTF-8 BOM straddling byte 512 of the file, inside a comment | enforces the policy as usual | silently reads no policy at all |

The full list is in [#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759).

### Why patching never ends

The Go side lines up by rewriting each processing path of the Python parser in Go: how merges expand, how empty keys are handled, how ordered mappings are built. Each path patched exposes the next one, and the processing order itself changes the outcome. [#2763](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/2763) took four rounds of fixes, and each round brought a new gap. Differential fuzzing finds gaps but cannot prove the two sides equal.

The root cause is not any single rule. It is "two parsers each read the file, and the results must match exactly".

## Decisions

### 1. Who parses what

| Config | The only parser | How other tools get it |
|---|---|---|
| Routing (`_routing_defaults`, `routing_profiles`, a tenant's `_routing`), domain policy, the tenant list | the route generator (Python) | read the JSON the generator outputs |
| Thresholds and the values the exporter actually serves | the exporter (Go) | unchanged: Python tools already read them as JSON from `da-guard served-values` / `effective` |

The second row is an existing precedent: thresholds already follow "one parser, other tools read its output". This ADR applies the same approach to routing and policy.

### 2. How it works

```text
tenant-api pod
┌──────────────────────────────┐          ┌──────────────────────────────┐
│ tenant-api (Go)              │ ───────▶ │ generator container (Python) │
│ - receives a write           │ validate │ - takes files at a commit    │
│ - writes or refuses by result│ ◀─────── │ - judges with the generator  │
│ - reads the resolved JSON    │  result  │ - outputs the resolved JSON  │
└──────────────────────────────┘          └──────────────────────────────┘
        the two talk only over a Unix socket in a shared directory
```

- The generator container uses the existing da-tools image, which already contains the generator.
- **How they talk**: no network port, only a Unix socket in a shared directory (emptyDir). Only processes that have that directory mounted and pass the socket file's permissions can connect.
- **How files are taken**: always at a given commit, from git objects (`git ls-tree` plus `git cat-file`), never from the shared working tree. Two reasons:
  - tenant-api's PR mode (writing by opening a PR instead of committing directly) switches branches in place in the working tree, so reading it directly can catch a switch half-way.
  - `git archive` applies the export rules in `.gitattributes`. If a customer's repo marks the policy file as not exported, the generator would not see it.
- **Caching**: the taken files are cached per commit; parse results are cached per file, keyed by "file content hash plus generator version".

### 3. How writes are validated

**The criterion is absolute**, as tenant-api's is today. The container replaces the file being written in the base tree with the new content and runs the generator. The write is refused if either:

- there is any violation whose subject is the tenant or file being written; or
- a tree-wide error appears that the base did not have, such as an unreadable file or a duplicate tenant.

**Why not compare "which error messages are new"**: when a tenant that already violates a policy rewrites its config, it produces exactly the same message line as the base, so comparing differences would let it through. Also, when a file cannot be read the generator prints a different kind of message, which string comparison misses.

So **the first step is to make the generator output structured findings**. A finding is one problem the generator reports, with fields for policy, tenant, file, kind, and whether it blocks. With these, the criterion no longer depends on comparing strings.

**Other rules**:

- **When the base already has errors**: a write is allowed as long as its own subject is clean and it adds no tree-wide error. One broken neighbour file cannot block everyone.
- **Which writes are validated**: every write that changes file content. Whether to validate is never decided by Go judging "did this touch routing".
- **What is validated is what is written**: the container returns the file path, content hash and base commit it validated. tenant-api checks all three under its write lock and revalidates on any mismatch; the bytes written must be exactly the bytes validated.
- **Batch writes**: one request carries the whole batch; the final state and every intermediate state are validated.
  - Today a batch patch is read by Go and written back out, so Go's reading ends up in the file. For example, the `!!merge q:` above is rewritten as an ordinary key `q:`.
  - In phase 1 Go still does the merge, but the merged content is always validated, so a violating result is blocked. What it cannot block is a rewrite that violates nothing but quietly changes the meaning; this is the other explicit phase-1 exception.
  - From phase 2, the generator container does batch merging.
- **Anything unexpected refuses**: a container error, a timeout or incomplete output all count as a refusal.
- **Error responses**: contain only the findings about the write's own subject, never another tenant's violations.

### 4. The resolved config (JSON)

Contents: whether each policy file is usable, each domain's tenants and constraints, each tenant's merged routing, and which file each tenant lives in.

| Rule | Why |
|---|---|
| The envelope carries a format version, the minimum reader version, the generator version and the commit it came from | a reader can tell whether it understands the file and whether it is current |
| A constraint kind the reader does not understand makes that policy unusable | ignoring an unknown constraint means blocking less |
| Duplicate keys, invalid UTF-8 and unknown fields are refused | the JSON layer must not grow its own second reading |
| Durations and thresholds are always output as strings | number precision would otherwise differ between the two languages |
| Write to a temp file, then rename | a reader never sees a half-written file |

If the latest generation fails, reads may keep using the last successful result, with three limits:
- reads only; writes are always validated live.
- per file; a policy file deleted in the new version must not be kept.
- past a time limit it becomes unusable and alerts.

### 5. Versions

Which da-tools version a customer's CI uses has nothing to do with tenant-api, so "exactly the same as the customer's CI" cannot be guaranteed. What can be guaranteed is: **tenant-api is no looser than the generator in its own container**.

When the two versions differ, a config reaches Alertmanager only if neither side refuses it:
- If the customer CI's generator is stricter, a write tenant-api accepted is blocked in CI. It is blocked one step later, not let through.
- If the generator in tenant-api's container is stricter, tenant-api refuses a write the CI would have accepted.

Neither case lets a violating config through; the cost is that the two verdicts can differ. To narrow the version gap:

- The tenant-api chart pins a da-tools version by default, and CI runs a compatibility test on that pair.
- The config `da-tools init` generates pins a version instead of `latest`.
- A policy file may declare the minimum generator version it needs; the container refuses writes when it is older. This is a new key, read only by the generator.
- tenant-api exposes the generator version as a metric.

### 6. Two-phase switch

| | How writes are judged | When the generator container is down | Reads |
|---|---|---|---|
| **Phase 1 (side by side)** | refused if Go or the generator refuses | writes that touch routing or policy get 503 | keep Go's reading, and record the differences between the two |
| **Phase 2 (generator only)** | the generator alone; the Go imitation code is deleted | writes get 503 | read the resolved JSON |

- **No fallback to Go alone when the container is down**:
  - Users who can send writes can influence whether the container is available, for example by sending many requests that each force a whole-tree recomputation.
  - Go judging alone has known loose spots.
  - Falling back to Go alone would let the requester pick the looser judge.
- **Phase-1 reads are an explicit exception to the acceptance condition**: reads keep the gaps listed in #2759 until phase 2. Writes must meet the acceptance condition from phase 1.
- tenant-api has an existing open switch, `--policy-unavailable-open`: when the policy file cannot be read, writes are still allowed. It keeps that meaning and does not cover a down container; there is no open switch for that.
- `tenant_api_policy_available` (the "is the policy usable" metric) is read by an alert, so it keeps its name and labels and represents the combined result. Each side's own value gets a new metric.

**When phase 2 starts**: on conditions, not on a number of days. All three must hold:

1. In CI, the two-way comparison over a fixed corpus plus a random corpus (minus entries already in the difference list) stays green for 14 consecutive days. The random seed changes every day and is logged on failure.
2. Side-by-side running lasts at least 14 days, spans one da-tools release (a `tools/v*` tag), and has real comparison volume:
   - comparisons reach a minimum count, set in the implementing PR and written back into this document;
   - one synthetic test write per day runs through the whole path;
   - counting days alone would pass with zero traffic, so both are required.
3. Every difference is in the **difference list** (below).

The **difference list** reuses the format of the existing `tests/rulepacks/vm_deviation_catalog.yaml`:

- Each entry: an example file, the cause, the direction, the disposition, a tracking issue and an expiry date.
- **The test computes the direction** by comparing the two verdicts, then checks it against the list; a wrong label fails. Nothing relies on a hand-written label.
- Checked both ways: a difference not on the list fails, and so does a listed one that has disappeared.
- **Any difference where Go is looser than the generator blocks phase 2**. Every loose class #2759 lists today is of this kind, so phase 2 cannot start until all of them are fixed.
- A difference where Go is stricter needs an explicit "the generator is authoritative" sign-off on its tracking issue. Once phase 2 starts, those writes become accepted.
- Every change to the list goes through a PR approved by the owner.

Customers' conf.d trees are not available, so these conditions cover the owner's trees only. A `da-tools` command lets customers run the same comparison on their own trees.

### 7. da-guard

da-guard stops judging routing and policy and leaves them to the generator's `--validate --strict`. Threshold checks are unchanged.

- **When da-guard runs on its own** (for example the standalone binary, with no Python):
  - Its report (text and JSON) states that routing and policy were "not judged", and it returns a dedicated exit code, so "not judged" and "judged and clean" can be told apart. The exit code value is set during implementation and documented in the CLI docs.
  - Migration: wherever CI runs da-guard, add a `da-tools generate-routes --validate --strict` step.
  - `--required-fields _routing.*` reports an error instead of silently stopping enforcement.
- **Before the Go checks are deleted**, checks only Go has are moved to the generator, added to the cross-check table of the two, and shown to make the tests fail when removed. The "only Go has it" list is computed, not maintained by hand. A check that was only a warning and becomes an error after the move is noted in the changelog as "stricter".
- `guard-defaults-impact.yml` also runs the generator.
- In phase 1, da-guard keeps its Go checks and CI runs both; phase 2 deletes them.

### 8. tenant-api read endpoints

- No GET endpoint returns routing rendered by the generator today, so there is nothing to migrate.
- Only when someone needs rendered routing is `GET /tenants/{id}/routing` added, always fed from the resolved JSON.
- `_routing` in the `/effective` response is the raw key the exporter merged, not rendered routing. The API description says so.

### 9. What phase 2 deletes

Decided by "is anyone still using it". A CI check is added **now**: it lists the users of the related packages and fails when they disagree with this table.

| Item | Action |
|---|---|
| Go code imitating Python's reading | deleted |
| changes in the vendored YAML parser made only for policy reading | deleted with it |
| a performance change in the vendored YAML parser that the exporter also uses | kept |
| helpers unrelated to parsing (such as tenant-id checks) | kept, or moved to another package |

Verified: with a tenant file containing a duplicate key, the generator reports `DuplicateKeyError` and refuses it, so deleting Go's duplicate-key check does not make anything looser.

## What happens on failure

| Situation | Behaviour |
|---|---|
| The generator container is down or times out | writes that touch routing or policy get 503; reads use the last successful result until its time limit |
| Generating the JSON fails (for example an invalid character in a policy file) | the last successful result is kept for reads; writes are still validated live |
| A reader meets a format version it does not know | the whole document is unusable |
| The container's generator is older than a policy requires | writes are refused |
| The base tree already has errors | writes with a clean subject go through |

## How it is accepted

- **Random-corpus comparison**: generate random conf.d trees → the generator outputs JSON and findings → the same tree goes to the Go readers → assert that the policies and tenants both sides enforce are **exactly the same**; reading an extra one and missing one both fail. Parsing the JSON either fully succeeds or refuses the whole document. The comparison code itself does not parse YAML.
  - From phase 2, writes and reads must match.
  - Phase 1 asserts writes only: the side-by-side verdict always blocks what the generator blocks.
- **Fixed corpus**: the existing `merge_key_policy_corpus.json` plus the examples from these issues:
  - [#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759): the remaining gaps in policy files.
  - [#2700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2700): `!!merge` keys in tenant files.
  - [#2713](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2713): an unparsable value inside an overridden merge source.
  - [#2674](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2674): a self-referencing YAML alias.
- Run in CI on the version pair the chart pins.

## Cost (measured 2026-10-09)

| Item | Measurement |
|---|---|
| The generator on 1000 tenants | 1.72 s, 38 MB; parsing takes 1.06 s, and every file is parsed twice (a cache removes that) |
| Parsing one file | 0.47 ms |
| Python startup | 0.11 s, 25 MB |
| Taking 1000 files from git | 0.21 s, 12 MB |
| Image size (compressed, amd64) | da-tools 36.9 MB; tenant-api 14.0 MB |

With a warm cache, a write costs mainly parsing one file plus the checks. Checks plus output take about 0.6 s; this is an estimate, not measured on its own.

## Rejected alternatives

Alternatives already argued in the body (keep imitating in Go, compare new error messages, take files with `git archive`, fall back to Go when the container is down, reuse the last result for writes) are not repeated here.

| Alternative | Why rejected |
|---|---|
| Commit a resolved copy of the policy into git | covers policy only; routing is still parsed by Go. If someone forgets to regenerate it, a freshly started tenant-api blocks every write, and a running one keeps enforcing the old policy |
| A custom config syntax shared by all three | older YAML rules read real tenant ids such as `no` and `0777` as booleans or numbers; and it is a breaking change |
| Switch the generator to a C-based parser | aligns only the layer that splits text into tokens; the gaps are in the layer that builds data from tokens |
| Port the whole Python parser to Go (about 4–5k lines) | considered only if the extra container's cost is unacceptable |
| A write API that accepts JSON only | a breaking change; Go would still convert JSON to YAML for the generator, the same problem in the other direction |
| da-guard calls the generator itself | needs Python at run time; the standalone binary without Python would stop working |

## Implementation order

1. The generator outputs structured findings.
2. CI checks: the user list for the phase-2 deletion scope, and the difference list with its two-way test.
3. The generator container: outputs the JSON and serves the validation endpoint; the Helm chart gains the container, socket and resource settings.
4. tenant-api: writes go through container validation (phase 1, side by side); reads move to the JSON.
5. da-guard: move the Go-only checks, add the "not judged" state and exit code; `guard-defaults-impact.yml` runs the generator.
6. Phase 2: delete the Go imitation code; re-test #2759, #2700, #2713 and #2674 on main, then close them.

## Appendix: implementation map

Code locations for implementers; skip when reading for the decision.

| Term in this document | Code |
|---|---|
| Go code imitating Python's reading | `shapeWork`, `pyResolve`, `ParseDomainPolicies`, `UnmarshalPolicy` in `pkg/routingpolicy` |
| tenant-api's current write verdict | `judgePutBody`; PR mode re-judges inside the `WritePRChecked` closure |
| batch patch rewritten by Go | `mergePatchYAML` |
| old "unreadable file counts as empty" behaviour (to be deleted) | `tenantBlockOnDisk` |
| changes in the vendored YAML parser | `third_party/yaml.v3`: `SpacesOnly` (policy only, deleted), nonspecific-tag (deleted when unused), `uniquekeys` (used by the exporter, kept); the pinning test `tests/ops/test_vendored_yaml_v3.py` is updated in the same PR |
| da-guard checks only Go has | `internal/guard/types.go` minus `tests/shared/routing_policy_parity_matrix.json` |
| the existing "known stricter" marker (folded into the difference list) | `known: stricter` in `merge_key_policy_corpus.json` |

## References

- [#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766): design discussion, measurements and external review record; [#2486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2486): tracking issue
- [ADR-023 tenant-api write plane: the single-writer invariant](./023-write-plane-single-writer-invariant.md) (Chinese only)
- Industry practice:
  - Parse once and hand validation to the authority: [Kubernetes server-side dry-run](https://kubernetes.io/blog/2019/01/14/apiserver-dry-run-and-kubectl-diff/).
  - Put the parsing tool in the consumer's pod: [Argo CD Config Management Plugins](https://argo-cd.readthedocs.io/en/stable/operator-manual/config-management-plugins/).
  - Run old and new side by side and switch on conditions: [GitHub Scientist](https://github.blog/developer-skills/application-development/scientist/).
  - Refuse rather than allow when the validator fails: [Kyverno policy settings](https://kyverno.io/docs/writing-policies/policy-settings/).
