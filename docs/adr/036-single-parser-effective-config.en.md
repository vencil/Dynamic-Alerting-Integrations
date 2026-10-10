---
title: "ADR-036: conf.d Is Parsed Once — Go Reads the YAML, the Generator Judges Routing and Policy"
tags: [adr, config, routing, domain-policy, tenant-api, security]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: en
---

# ADR-036: conf.d Is Parsed Once — Go Reads the YAML, the Generator Judges Routing and Policy

> **Language / 語言：** [中文](./036-single-parser-effective-config.md) | **English (Current)**

## Status

✅ **Accepted** (drafted and approved by the owner 2026-10-09).

- **Revised 2026-10-11**: the YAML syntax layer also keeps a single parser. The original version let the route generator (Python) read the YAML itself while the Go side read it again, so the differences between two YAML parsers still had to be lined up one by one. After the revision every tool reads the same JSON parsed by Go, and the generator is responsible only for routing and policy semantics. The direction was approved by the owner in [#2858](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2858); this text is reviewed in its PR.
- The design and the original text each went through one round of external adversarial review (by a different model); the revision went through one round of internal adversarial review (by a different model).

## Summary

**Problem**: the same conf.d is read by two YAML parsers: the Go side (exporter, tenant-api, da-guard) uses yaml.v3, the Python side (the route generator and the tools) uses PyYAML. They often read the same file differently; both sides wrote a lot of code to imitate the other, and several rounds of fixes have not closed the gap.

**Decisions**:

1. **Only Go parses the YAML.** da-guard gains a command that reads a whole conf.d into one JSON document: which files were selected, the content of each file (keys always as their source text, scalars carrying both source text and type), and which files could not be read. The Python side (including the route generator) no longer parses conf.d YAML itself; it reads this JSON.
2. **Each semantic area has one implementation.** Routing, domain-policy and tenant-list semantics are judged only by the generator (Python); thresholds and the values the exporter serves only by the exporter (Go).
3. The tenant-api pod gains a container that runs the generator and validates every write that touches routing or policy. tenant-api no longer judges these semantics itself.
4. The switch happens in two phases: in phase 1 old and new run side by side and a write is refused if either side refuses it; once the conditions are met, phase 2 deletes the Go code that imitates the generator's semantics.
5. da-guard stops judging routing and policy; the generator judges them.

**Impact on users and operators** (all breaking changes):

- Values written under the old YAML rules may change meaning. For example `send_resolved: yes` in a route used to be read by the generator as the boolean `true`; parsed by yaml.v3 it is the string `"yes"`. A one-off migration audit command lists every affected value.
- Python tools that read conf.d need da-guard. The da-tools image already bundles it; local pre-commit builds it with Go on the spot.
- tenant-api gains a container (the da-tools image). When it is unavailable, writes that touch routing or policy get 503; they are not let through.
- A CI that runs only da-guard no longer covers routing and policy, and needs an extra `da-tools generate-routes --validate --strict` step.

**Non-negotiable acceptance condition**: no reader may be looser than the generator. It must neither read a policy the generator drops nor miss one the generator enforces. From phase 2 this holds for writes and reads; in phase 1 it holds for writes only.

## Problem

### An example

YAML has a way to "merge another mapping in", usually written `<<:`. PyYAML treats any key tagged `!!merge` as such a merge; yaml.v3 recognises only a literal `<<`. This tenant file falls into that gap:

```yaml
# t1.yaml
tenants:
  t1:
    _routing:
      !!merge q: {receiver: {type: slack, api_url: "https://hooks.slack.com/x"}}
```

- **The generator** applies the merge, so t1's receiver is `slack`. If the domain policy forbids slack, the generator reports an error and exits 1.
- **da-guard** treats `q` as an ordinary key, so the receiver falls back to the platform default. It judges the config compliant and exits 0.

Same file, opposite verdicts, and the Go side is the one that lets it through.

### Not just one example

| Written as | PyYAML (generator) | yaml.v3 (Go side) |
|---|---|---|
| A key tagged `!!merge` in a tenant's `_routing` | merged in | an ordinary key |
| `send_resolved: yes`, `on`, `off` | boolean | string |
| `a: "x\/y"` (`\/` inside a quoted string) | `x/y` | the whole file is unreadable |
| a TAB after `a:`, then the value | the whole file is unreadable | read normally |
| A policy file nested 600 levels deep | the whole file is unusable | the policy is read and enforced |
| A UTF-8 BOM straddling byte 512 of the file, inside a comment | the policy is enforced as usual | silently reads no policy at all |

The full list for policy files is in [#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759).

### Why the fixes never end

Lining up means one side rewrites, in its own language, every code path of the other side's parser: the Go side has a package dedicated to imitating PyYAML, and the vendored yaml.v3 carries two patches that exist only to imitate PyYAML; the Python side has several loaders that imitate yaml.v3 (for example reading mapping keys as their source text). Each fix exposes the next path, and the order of processing itself affects the result. Differential fuzzing finds gaps, but it cannot prove the two sides equal.

The original version handled only the semantic layer: routing and policy are judged only in the generator. But the generator still read the YAML with PyYAML and the Go side with yaml.v3, so syntax-layer gaps like the first four rows of the table remained — they just became "the generator container and tenant-api disagree".

Another route was tried: restrict conf.d to what "the two parsers are guaranteed to read the same way". Measured, it does not work: in rows three and four of the table the values are already quoted and no advanced feature is used, and the two sides still disagree. Such a "same subset" can only be sampled, never defined.

The root cause is "two parsers each read once, and the results must be identical". The only approach that needs no proof of equality is to keep one parser.

## Decision

### 1. Who parses what

| Layer | The single implementation | How other tools get it |
|---|---|---|
| YAML syntax: which files are selected, the content of each file | Go (da-guard, yaml.v3) | read the "parse-result JSON" da-guard outputs |
| Semantics of routing (`_routing_defaults`, `routing_profiles`, a tenant's `_routing`), domain policy and the tenant list | the route generator (Python) | read the generator's "resolved config JSON" and findings |
| Thresholds and the values the exporter actually serves | the exporter (Go) | unchanged: read the JSON of `da-guard served-values` / `effective` |

The third row is the existing precedent: thresholds already follow "one implementation, other tools read its output". This ADR applies the same approach to the YAML syntax layer and to routing and policy semantics.

### 2. The parse-result JSON

da-guard gains a `parse-tree` subcommand. It takes a conf.d directory and outputs one JSON document:

- **Which files were selected**: by the exporter's own file-selection rules (extension, case, hidden files, subdirectories). The Python side no longer has its own copy of these rules.
- **The content of each file**: mapping **keys are always output as their source text** (`010` is `"010"`, the way the exporter reads tenant ids); **scalars carry both their source text and the type yaml.v3 resolved**, for example `{"text": "yes", "tag": "!!str"}`. The source text is there because the exporter reads thresholds as text; the type is there for the semantic layers that need booleans or numbers. Values JSON cannot represent directly, such as timestamps or infinity, are likewise expressed only as text plus type and lose nothing.
- **Unreadable files**: file name and reason (syntax error, read failure, not a regular file); no content.
- The top level carries a format version and the da-guard version. A reader that does not recognise the format version treats the whole document as unusable.

Example. Input:

```yaml
# t010.yaml
tenants:
  010:
    _routing:
      receiver:
        type: slack
        api_url: "https://hooks.slack.com/x"
        send_resolved: yes
      group_wait: 30s
```

Output (excerpt; measured by converting yaml.v3's nodes to JSON one by one):

```json
{
  "t010.yaml": {
    "tenants": {
      "010": {
        "_routing": {
          "group_wait": {"text": "30s", "tag": "!!str"},
          "receiver": {
            "api_url": {"text": "https://hooks.slack.com/x", "tag": "!!str"},
            "send_resolved": {"text": "yes", "tag": "!!str"},
            "type": {"text": "slack", "tag": "!!str"}
          }
        }
      }
    }
  }
}
```

The same file read with PyYAML gives `{'tenants': {8: {'_routing': {'receiver': {'type': 'slack', 'api_url': 'https://hooks.slack.com/x', 'send_resolved': True}, 'group_wait': '30s'}}}}`: the tenant id becomes the integer 8 and `yes` a boolean. Once they read the JSON, every Python tool sees exactly what the Go side read.

**How the Python side gets it**: through the existing "Python calls a Go binary and reads its JSON" mechanism (da-tools already calls da-guard's `served-values` / `effective` this way). The shared YAML-reading and file-selection libraries read this JSON instead; the tools no longer call a YAML parser on conf.d directly. A tool that rewrites files may keep its own text-level editing (to keep comments and layout), but what it decides on always comes from the JSON, and its rewritten result is read again through `parse-tree` to confirm it.

**Scope**: conf.d only. Rule packs, Kubernetes manifests and other YAML are out of scope for this ADR.

### 3. How it runs (tenant-api)

```text
tenant-api pod
┌──────────────────────────────┐      ┌──────────────────────────────┐
│ tenant-api (Go)              │ ───▶ │ generator container           │
│ - receives the write request │ check│   (image: da-tools)           │
│ - writes or refuses by the   │ ◀─── │ - checks out files by commit  │
│   verdict                    │result│ - da-guard parse-tree parses  │
│ - reads the resolved JSON    │      │ - generator judges semantics  │
└──────────────────────────────┘      └──────────────────────────────┘
          They talk only through a Unix socket in a shared directory
```

- The generator container uses the existing da-tools image, which already contains the generator and da-guard.
- **Communication**: no network port; only a Unix socket in a shared directory (emptyDir). Only processes that mount that directory and pass the socket file permissions can connect.
- **Getting the files**: always from git objects at a given commit (`git ls-tree` plus `git cat-file`), never from the shared working directory, for two reasons:
  - tenant-api's PR mode (opening a PR per write instead of committing directly) switches branches in place in the working directory, so a direct read can see a half-switched state.
  - `git archive` applies the export rules in `.gitattributes`. If a customer's repo marks the policy file as not exported, the generator would not see it.
- **Caching**: checked-out files are cached by commit; parse results per file by "file content hash plus da-guard version"; semantic verdicts by "parse-result hash plus generator version".

### 4. How writes are validated

**The criterion is absolute**, the same as tenant-api today. The container replaces, in the base tree, the file being written with the new content, parses it and then runs the generator. The write is refused if either holds:

- There is any violation whose subject is the tenant or file being written.
- A tree-level error appears that the base did not have, such as an unreadable file or a duplicate tenant.

**Why not compare "which error messages are new"**: when a tenant that already violates the policy changes its config, it produces exactly the same message line as the base, and a diff would let it through. Also, when a file cannot be read the generator prints a message in a different format, which string comparison would miss. So the criterion is based on the generator's structured findings: each problem carries the policy, tenant, file, kind, and whether it blocks.

**Other rules**:

- **When the base itself already has errors**: the write is allowed as long as its subject has no problem and it adds no tree-level error. One broken neighbour file must not block everyone.
- **Which writes are validated**: every write that changes file content. Whether to validate is not decided by Go guessing "does this touch routing".
- **What was validated is exactly what is written**: the container returns the "file path, content hash, base commit" it validated. tenant-api checks all three under the write lock and revalidates on any mismatch; the bytes written must be exactly those validated.
- **Batch writes**: one request carries the whole batch of files; both the final state and every intermediate state are validated. Today a batch patch is read by Go and written back out; since only Go parses the YAML, writing it back no longer bakes "the other parser's reading" into the file, but it can still change layout and comments, so the merged content is always validated. From phase 2, batch merging is done by the generator container.
- **Any anomaly is a refusal**: a container error, a timeout, incomplete output — all count as refusals.
- **Error responses**: only the findings for the subject of this write, never other tenants' violations.

### 5. The resolved config (JSON)

The generator's semantic output includes: whether each policy file is usable, each domain's tenants and constraints, each tenant's merged routing, and which file each tenant comes from.

| Rule | Reason |
|---|---|
| The top level carries a format version, minimum reader version, generator version, da-guard version and the commit it corresponds to | A reader can tell whether it understands the document and whether it is reading the latest |
| A policy with a constraint kind the reader does not understand is treated as unusable | Ignoring an unknown constraint means blocking less |
| No duplicate keys, invalid UTF-8 or unknown fields | So the JSON layer does not grow two readings of its own |
| Durations and thresholds are always output as strings | So number precision cannot differ between the two languages |
| Written to a temporary file, then renamed | A reader never sees a half-written file |

When the latest generation fails, reads may keep using the last successful result, with three limits:
- Reads only; writes are always validated live.
- Per file; a policy file the new version deleted must not be kept.
- Past a time limit it is treated as unusable and alerts.

### 6. Versions

Which da-tools version a customer's CI runs is independent of tenant-api, so "exactly matching the customer's CI" is not achievable. What is guaranteed is: **tenant-api is never looser than the generator in its own container**. da-guard and the generator ship in the same da-tools image, so their versions are decided together.

When the two sides run different versions, a refusal on either side keeps the config out of Alertmanager:
- If the customer's CI version is stricter, a write tenant-api accepted is blocked in CI. It is blocked one step later, never let through.
- If the version in tenant-api's container is stricter, tenant-api refuses writes the CI would have accepted.

Neither case lets a violating config through; the cost is that the two sides can disagree. To narrow the version gap:

- tenant-api's chart pins a default da-tools version, and CI runs compatibility tests against that pair.
- The config generated by `da-tools init` pins a version instead of `latest`.
- A policy file can declare "requires at least generator version X"; a container older than that refuses writes. This is a new key, read only by the generator.
- tenant-api exposes the generator and da-guard versions as metrics.

### 7. Two-phase switch

| | How writes are judged | When the generator container is down | Reads |
|---|---|---|---|
| **Phase 1 (side by side)** | Refused if either Go or the generator refuses | Writes touching routing or policy get 503 | Keep Go's reading; record differences between the two |
| **Phase 2 (generator only)** | Only the generator; the Go code imitating the generator is deleted | Writes get 503 | Read the resolved JSON |

- **No falling back to Go alone when the container is down**:
  - Anyone who can send writes can affect whether the container is available, for example by sending many requests that need whole-tree recomputation.
  - Go judging alone has known loose spots.
  - Falling back to Go alone would let the requester pick the looser judge.
- **Phase 1 reads are a stated exception to the acceptance condition**: reads keep known gaps until phase 2. Writes must meet the acceptance condition from phase 1.
- tenant-api has an escape hatch `--policy-unavailable-open`: when the policy file cannot be read, writes are still allowed. It keeps its meaning and does not cover the container being down; there is no escape hatch for that.
- `tenant_api_policy_available` (the "is policy usable" metric) is read by alerts; it keeps its name and labels and represents the combined result of both sides. Each side's value gets a new metric.
- **The generator must read the parse-result JSON before phase 1.** Phase 1 then compares only semantic-layer differences; syntax-layer differences (the kind in the table above) are gone before comparison starts.

**When phase 2 starts**: by conditions, not by a date. All three must hold:

1. In CI, the bidirectional comparison on a fixed corpus plus a random corpus (minus entries already in the divergence catalog) is green for 14 consecutive days. The random corpus seed changes daily and is recorded on failure.
2. Side-by-side running in production lasts at least 14 days, spans one da-tools release (a `tools/v*` tag), and has real comparison volume:
   - The number of comparisons reaches a floor. The exact number is set in the implementation PR and written back into this document.
   - One synthetic test write per day goes through the full flow.
   - Days alone would be satisfied by zero traffic, so both are required.
3. Every difference is listed in the **divergence catalog** (below).

The **divergence catalog** reuses the format of the repo's existing `tests/rulepacks/vm_deviation_catalog.yaml`:

- Each entry: sample file, reason, direction, disposition, tracking issue, expiry date.
- **The direction is computed by a test**: derived by comparing the two sides' verdicts, then checked against the catalog value; a wrong label goes red. Not hand-written.
- Two-way guard: a difference not in the catalog goes red; an entry whose difference has actually disappeared also goes red.
- **Any difference where Go is looser than the generator blocks phase 2.**
- A difference where Go is stricter than the generator needs an explicit "the generator wins" sign-off on its tracking issue. After phase 2 those writes become accepted.
- Every change to the catalog goes through a PR. Each entry pins exact row counts, so any addition or removal shows up in the PR diff for the reviewer to see; nothing currently forces owner approval (main's branch protection does not require review; CODEOWNERS only notifies).
- Once the generator reads the parse-result JSON, every syntax-layer entry in the catalog (for example `!!merge` keys, BOM, nesting depth) should disappear; one that does not means some reader still has not switched to the JSON.

The customers' conf.d trees are not available, so these conditions cover only the owner's tree. A `da-tools` command is also provided so customers can run the same comparison on their own tree.

### 8. Migration: values whose reading changes

Once yaml.v3 parses the YAML, any value the two parsers read differently changes for the generator and the Python tools. The most common are unquoted `yes` / `no` / `on` / `off` (previously booleans, now strings) and keys tagged `!!merge` (previously merged, now ordinary keys).

- da-tools provides a one-off migration audit command: it reads the customer's tree with both parsers and lists every value read differently, with its file, line and suggested spelling (for example rewriting a boolean as `true` / `false`). This command is the only place in this ADR that still reads conf.d with PyYAML; it only lists differences and judges nothing.
- The release notes list the affected spellings and the migration steps.
- The exporter already uses yaml.v3, so how thresholds are read does not change.

### 9. da-guard

da-guard gains `parse-tree` and stops judging routing and policy; the generator's `--validate --strict` judges them. Threshold checks are unchanged.

- **When da-guard runs alone** (for example the standalone binary without Python):
  - The report (text and JSON) states that routing and policy were "not judged", and returns a dedicated exit code, so "not judged" and "judged, no problem" can be told apart. The exit code value is set during implementation and documented in the CLI reference.
  - Migration: wherever CI ran da-guard, add a `da-tools generate-routes --validate --strict` step.
  - `--required-fields _routing.*` errors out instead of silently doing nothing.
- **Before deleting Go checks**, move the checks only Go has into the generator, add them to both sides' comparison table, and prove the tests go red without them. The "only Go has" list is computed by code, not maintained by hand. A check that was a warning and becomes an error after the move is noted as "stricter" in the changelog.
- `guard-defaults-impact.yml` also runs the generator.
- In phase 1 da-guard keeps its Go checks and CI runs both; phase 2 deletes them.

### 10. tenant-api read endpoints

- No GET endpoint returns generator-rendered routing today, so there is nothing to migrate.
- `GET /tenants/{id}/routing` is added only when someone needs rendered routing, and always serves the resolved JSON.
- The `_routing` in `/effective` responses is the raw key as the exporter merged it, not rendered routing. The API description will say so.

### 11. What gets deleted

Decided by "is anyone still using it". CI already has a check: it lists the users of the relevant packages and goes red when they differ from the declaration table.

| Item | When | Action |
|---|---|---|
| Python loaders that imitate yaml.v3, the Python copy of the file-selection rules | after every Python tool that reads conf.d reads the parse-result JSON | delete |
| The hand-written line-by-line YAML parsing in the portal config checker | same; it calls tenant-api's validation endpoint instead | delete |
| The two vendored yaml.v3 patches that exist only to imitate PyYAML (non-specific tag, spaces only) | after the generator reads the parse-result JSON | delete; recorded as a breaking change |
| The vendored yaml.v3 performance change the exporter also uses (linear duplicate-key detection) | — | keep |
| Go code that imitates the generator's semantics (the PyYAML-decoding imitation package, the Go copy of routing and policy judging) | phase 2 | delete |
| Small utilities unrelated to parsing (for example the tenant id check) | — | keep or move to another package |

Verified: a tenant file with a duplicate key makes the generator report a duplicate-key error and refuse. So deleting Go's duplicate-key check does not make anything looser.

## When things fail

| Situation | Behaviour |
|---|---|
| The generator container is down or times out | Writes touching routing or policy get 503; reads keep the last successful result until the time limit |
| `parse-tree` cannot read a file | The file is listed under unreadable files; a write whose subject it is gets refused, otherwise it is handled as "the base already has errors" |
| Generating the resolved JSON fails (for example an invalid character in a policy file) | The last successful result is kept for reads; writes are validated live as usual |
| A reader meets an unknown format version | The whole document is treated as unusable |
| The generator in the container is older than a policy requires | Writes are refused |
| No da-guard locally and no Go to build it | Python tools that read conf.d and the pre-commit hooks fail with the reason; they never fall back to parsing on their own |
| The base tree already has errors | Writes with a clean subject go through |

## How it is accepted

- **No second YAML parser**: a CI check lists every call site on the Python side that reads conf.d and allows only the parse-result JSON (except the migration audit command).
- **Random-corpus comparison**: generate a random conf.d → `parse-tree` → the generator outputs the resolved JSON and findings → hand the same parse result to the Go readers → assert both enforce **exactly the same** policies and tenants; reading more or less both count as failures. JSON parsing either fully succeeds or rejects the whole document. The comparison code itself parses no YAML.
  - From phase 2, writes and reads must both match.
  - Phase 1 asserts writes only: the side-by-side verdict always blocks a write the generator would block.
- **Fixed corpus**: the existing `merge_key_policy_corpus.json`, plus the samples from:
  - [#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759): the remaining gaps for policy files.
  - [#2700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2700): `!!merge` keys in tenant files.
  - [#2713](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2713): an unparseable value inside an overridden merge source.
  - [#2674](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2674): a self-referencing YAML alias.
- In CI, run against the version pair the chart pins.

## Cost

Measured for the original version (2026-10-09):

| Item | Measurement |
|---|---|
| Generator over 1000 tenants | 1.72 s, 38 MB; parsing is 1.06 s of that, and each file is parsed twice (a cache can save this) |
| Parsing one file | 0.47 ms |
| Python startup | 0.11 s, 25 MB |
| Checking 1000 files out of git | 0.21 s, 12 MB |
| Image size (compressed, amd64) | da-tools 36.9 MB; tenant-api 14.0 MB |

Measured for the revision (2026-10-11, 1000 files with one tenant each, about 20 lines per file including `_routing`, `_metadata` and a scheduled threshold):

| Item | Measurement |
|---|---|
| yaml.v3 parsing each file into nodes and converting to the JSON format above (an experimental program standing in for the not-yet-implemented `parse-tree`) | about 0.11 s, 29 MB allocated in total, 795 KB of output |
| Python reading that JSON | 0.024 s |
| For comparison: PyYAML reading the same 1000 files | 1.22 s |

After switching to the JSON, the Python side's parsing cost falls to roughly a tenth or less of what it was.

## Considered and rejected

The options whose reasons are already given in the body (keep both sides imitating each other, diff error messages, check out with `git archive`, fall back to Go when the container is down, reuse the last result for writes) are not repeated.

| Option | Why rejected |
|---|---|
| Restrict conf.d to what "the two parsers are guaranteed to read the same way" | Measured, it does not work: `\/` inside a quoted string and a TAB after a colon are read differently even with the value quoted and no advanced feature used. Such a subset can only be sampled, never defined |
| Let Python parse all YAML and have the Go side read Python's JSON | The exporter is a runtime component that must run standalone in Go; depending on Python would put a Python runtime in every exporter pod |
| Commit the resolved policy into git as a separate file | Covers policy only; routing is still parsed by both sides. If someone forgets to regenerate it, a freshly started tenant-api blocks every write because it cannot read it, and a running one keeps the old policy |
| A custom config syntax shared by all three | Old YAML rules read real tenant ids such as `no` or `0777` as booleans or numbers; and every customer would have to rewrite every config file |
| Make the generator use a C-implemented parser | Only lines up the layer that splits text into tokens; gaps also sit in the layer that builds data from tokens |
| Port all of Python's routing and policy semantics to Go | About four to five thousand lines, a second semantic implementation; only considered if the cost of the extra container is unacceptable |
| Make the write API accept only JSON | Solves only tenant-api writes; customers' hand-written YAML still has to be parsed in CI and by the exporter |
| Have da-guard call the generator itself | Needs Python at runtime; the standalone binary without Python could no longer be used |

## Implementation order

Done: the generator outputs structured findings; CI checks (users of the deletion scope, the divergence catalog and its two-way tests).

Next:

1. da-guard `parse-tree`; the shared Python libraries read it; the Python tools and pre-commit hooks go through it; the migration audit command.
2. The generator reads the parse-result JSON; delete the Python loaders that imitate yaml.v3 and the two imitation patches in the vendored yaml.v3.
3. The generator container: outputs the resolved JSON and serves the validation endpoint; the Helm chart adds the container, socket and resources.
4. tenant-api: writes are validated by the container (phase 1, side by side); reads use the resolved JSON.
5. da-guard: move the checks only Go has, add the "not judged" state and exit code; `guard-defaults-impact.yml` also runs the generator.
6. Phase 2: delete the Go code that imitates the generator's semantics, re-run #2759 and the other fixed corpus on main, and close the issues.

## Appendix: code locations

Where the code is, for implementers; can be skipped when reading the decision.

| Term in this document | Code |
|---|---|
| The existing "Python calls a Go binary and reads its JSON" mechanism | `scripts/tools/_lib_godispatch.py`, `scripts/tools/_lib_tenant_values.py` (`served-values` / `effective`) |
| The Python YAML-reading and file-selection libraries (switch to `parse-tree`) | `scripts/tools/_lib_io.py`, `_lib_confd.py`, `_lib_yaml_keys.py` |
| Python loaders that imitate yaml.v3 | `_lib_yaml_keys.ExporterKeyLoader`, the `StrictExporterKeyLoader` family in `_lib_io`, `_GoKeyLoader` in `describe_tenant` |
| The portal's hand-written line-by-line parsing | `tools/portal/src/interactive/tools/config-lint/lint.js` |
| Go code that imitates the generator's semantics | `shapeWork`, `pyResolve`, `ParseDomainPolicies`, `UnmarshalPolicy` in `pkg/routingpolicy`; `pkg/pyyamlcompat` |
| Changes in the vendored yaml.v3 | `third_party/yaml.v3`: spaces only (policy only; delete), non-specific tag (delete), linear duplicate-key detection (used by the exporter; keep); the pinning test `tests/ops/test_vendored_yaml_v3.py` is updated in the same PR |
| tenant-api's current write judging | `judgePutBody`; in PR mode re-judged inside the `WritePRChecked` closure |
| Go re-emitting batch patches | `mergePatchYAML` |
| The old "unreadable file counts as empty" behaviour (to be deleted) | `tenantBlockOnDisk` |
| The deletion-scope user check (CI) | declaration table `tests/shared/phase2_deletion_scope.json`; the test comparing it, `components/threshold-exporter/app/phase2_deletion_scope_test.go` |
| Checks only Go's da-guard has | `internal/guard/types.go` minus `tests/shared/routing_policy_parity_matrix.json` |
| The divergence catalog | `tests/shared/reader_divergence_catalog.yaml` (policy and routing layers); the Go verdict snapshot `tests/shared/merge_key_go_verdicts.json`; direction computation, two-way comparison, expiry and phase-2 readiness in `tests/shared/test_reader_divergence_catalog.py`, run with strict expiry by `make pre-tag` via `reader-divergence-expiry` |

## References

- [#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766): design discussion, measurements and external review of the original version; [#2858](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2858): tracking issue for the revision
- [ADR-023 tenant-api write plane: the single-writer invariant](./023-write-plane-single-writer-invariant.md) (Chinese only)
- Industry practice:
  - Parse once, leave validation to the authoritative side: [Kubernetes server-side dry-run](https://kubernetes.io/blog/2019/01/14/apiserver-dry-run-and-kubectl-diff/).
  - Put the parsing tool into the user's pod: [Argo CD Config Management Plugins](https://argo-cd.readthedocs.io/en/stable/operator-manual/config-management-plugins/).
  - Run old and new side by side, switch by conditions: [GitHub Scientist](https://github.blog/developer-skills/application-development/scientist/).
  - Refuse rather than allow when the validator fails: [Kyverno policy settings](https://kyverno.io/docs/writing-policies/policy-settings/).
