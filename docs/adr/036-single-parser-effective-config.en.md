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

- **Revised 2026-10-11 (approved by the owner)**: the YAML syntax layer also keeps a single parser, and every read used to judge config content reads the JSON parsed by Go (exceptions in §2 and §8); a write goes ahead only when the whole tree afterwards passes the generator's check; no resolved config JSON; `--policy-unavailable-open` is removed; git is required.
- **Revised again 2026-10-11 (approved by the owner)**: five steps merged in order on main, each releasable (§7); sets the `parse-tree` output envelope, its closed "unreadable" list and fixed corpus (§2), the error code when the generator container is unavailable (§5), and da-guard's "not judged" exit code (§9).
- **Third revision 2026-10-12 (approved by the owner)**: from step 1 the generator container caches parse results across requests and runs each judgement in a separate process with memory and time limits, and sets the timeouts and the line between "refuse this write" and "container unavailable" (§3); batches become all-or-nothing, released on their own before step 1, and are judged and committed in a temporary git worktree (§4, §7).
- **Revision 2026-10-14**: the parse cache key uses the SHA-256 of the file's content instead of git's blob SHA (§3).
- **Revision 2026-10-15 (owner decision)**: when files are read from git objects, every entry that is not a regular file (a symlink, a gitlink, …) is judged `not_regular_file` (§3).
- **Revision 2026-10-15 (approved by the owner)**: merges of batch patches and custom-alerts are guarded by an invariant on the result: outside the edit, every node of the result must be written exactly as in the original (§4).
- **Revision 2026-10-16 (approved by the owner)**: "not judged" now also covers a tree with `_` keys that only the generator judges (§9), to go with [ADR-038](./038-unread-keys-judged-by-readers.en.md).
- **Revision 2026-10-16 (approved by the owner)**: a file `parse-tree` treats as unreadable while the exporter reads it is listed as "not judged" evidence (exit 4), not exit 3; records the known window before step 4 in which a file only PyYAML refuses passes da-guard, and re-evaluates exit 4 when step 4 is done (§9).
- Both the design and the revision went through adversarial review (by a different model).

## Summary

**Problem**: the same conf.d (the tenant config directory) is read by two YAML parsers: the Go side (exporter, tenant-api, da-guard) uses yaml.v3, the Python side (the route generator and the tools) uses PyYAML. They often read the same file differently; both sides wrote a lot of code to imitate the other, and several rounds of fixes have not closed the gap.

**Decisions**:

1. **Only Go parses the YAML.** da-guard gains a command that reads a whole conf.d into one JSON document: which files were selected, the content of each file, and which files could not be read. When judging config content, the Python side (including the route generator) no longer parses conf.d YAML itself; it reads this JSON. The only exceptions are the migration audit command in §8 and the writing tools in §2.
2. **Each semantic area has one implementation.** Routing, domain-policy and tenant-list semantics are judged only by the generator (Python); thresholds and the values the exporter serves only by the exporter (Go).
3. The tenant-api pod gains a container that runs the generator, and every write that changes file content asks it first. tenant-api no longer judges routing and policy itself.
4. **Merged in order on main, every step releasable.** The changes above are split into five steps merged in order (§7): tenant-api first asks the generator container, then da-guard stops judging routing and policy, then the Go imitation code is deleted, and only last do the generator and the Python tools read the JSON. Whichever commit is released, there is no release where the generator already reads with yaml.v3 while Go still imitates PyYAML. There is no side-by-side period and no switch.

**Impact on users and operators** (all breaking changes):

- Values written under the old YAML rules may change meaning, and some files become unreadable. For example `send_resolved: yes` in a route used to be read by the generator as the boolean `true`; parsed by yaml.v3 it is the string `"yes"`. A one-off migration audit command lists every affected value and file.
- Python tools that read conf.d need da-guard. The da-tools image already bundles it; local pre-commit builds it with Go on the spot.
- tenant-api gains a container (the da-tools image). When the container or the deployment policy file is unavailable, every write that changes file content gets 503 (thresholds-only writes included); there is no switch to let writes through while unavailable.
- The whole tree after a write must pass the generator's check before the write goes ahead. While anything in the tree would be blocked, every write through tenant-api is refused until it is fixed (§4).
- Batch writes become all-or-nothing, released on their own before step 1 (§4).
- tenant-api requires git; deployments without git are no longer supported.
- Some files today's generator refuses are read per yaml.v3 from §7 step 4 instead of being refused whole (§2). The migration audit command lists each such file and what it will be read as.
- A CI that runs only da-guard no longer covers routing and policy, and needs an extra `da-tools generate-routes --validate --strict` step.

**Non-negotiable acceptance conditions**:

- No reader may be looser than the generator: it must neither read a policy the generator drops nor miss one the generator enforces. tenant-api's write criterion is the generator's own result (§4), so for tenant-api this holds by definition.
- `parse-tree`'s "unreadable" is a closed list (§2): it names only the forms different readers could read with different meanings; everything else is read per yaml.v3, including files today's generator refuses only because of PyYAML's limits. The migration audit command lists those files one by one before the switch (§8).

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

Lining up means one side rewrites, in its own language, every code path of the other side's parser: the Go side has a package dedicated to imitating PyYAML, and the vendored (copied into the repo and modified) yaml.v3 carries two patches added to imitate PyYAML; the Python side has several loaders that imitate yaml.v3 (for example reading mapping keys as their source text). Each fix exposes the next path, and the order of processing itself affects the result. Feeding the same random input to both sides and comparing finds gaps, but it cannot prove the two sides equal.

Converging only the semantics in one place is not enough. If routing and policy are judged only in the generator, but the generator still reads the YAML with PyYAML and the Go side with yaml.v3, syntax-layer gaps like the first four rows of the table remain — they just become "the generator container and tenant-api disagree".

Another route was tried: restrict conf.d to what "the two parsers are guaranteed to read the same way". Measured, it does not work: in rows three and four of the table the values are already quoted and no advanced feature is used, and the two sides still disagree. Such a "same subset" can only be sampled, never defined.

The root cause is "two parsers each read once, and the results must be identical". The only approach that needs no proof of equality is to keep one parser.

## Decision

### 1. Who parses what

| Layer | The single implementation | How other tools get it |
|---|---|---|
| YAML syntax: which files are selected, the content of each file | Go (da-guard, yaml.v3) | read the "parse-result JSON" da-guard outputs |
| Semantics of routing (`_routing_defaults`, `routing_profiles`, a tenant's `_routing`), domain policy and the tenant list | the route generator (Python) | read the generator's findings (each problem the generator reports, see §4) and its status endpoint (§5) |
| Thresholds and the values the exporter actually serves | the exporter (Go) | unchanged: read the JSON of `da-guard served-values` / `effective` |

The third row is the existing precedent: thresholds already follow "one implementation, other tools read its output". This ADR applies the same approach to the YAML syntax layer and to routing and policy semantics.

### 2. The parse-result JSON

da-guard gains a `parse-tree` subcommand. It takes a conf.d directory and outputs one JSON document. It also has a single-file mode, which file-writing tools use to check their output before writing (see the end of this section).

**Which files were selected**: every file some reader actually reads, marked with who reads it.

- The exporter's file-selection rules (extension, case, hidden files, subdirectories), including which `_defaults` file each directory finally uses.
- Files the generator reads and the exporter does not: policy and routing-profile files (`_domain_policy.yaml`, `_routing_profiles.yaml`, `_profiles.yaml`, including those in subdirectories and the `.yml` spelling). When both spellings of one file exist, the generator's current handling applies.

The Python side no longer has its own copy of these rules. Differences between the two sides' selection go into the fixed corpus.

**The content of each file**: the output is yaml.v3's structure **after decoding**, not the raw nodes.

- The top level must be a mapping; an empty file counts as an empty mapping.
- YAML aliases (`*x`, pointing back at content marked `&x`) and unquoted merge keys `<<` (the spelling that merges another mapping in) are expanded, under the same alias limit yaml.v3 applies when decoding; a file over the limit is listed as unreadable. A quoted `"<<"` is an ordinary key and is output as written. Readers need not expand anything.
- mapping **keys are always output as their source text** (`010` is `"010"`, the way the exporter reads tenant ids).
- **Scalars carry their source text, the type yaml.v3 resolved, and the converted value**: for example `{"text": "yes", "tag": "!!str", "value": "yes"}`, `{"text": "0777", "tag": "!!int", "value": "511"}`.
  - Booleans and null are JSON `true` / `false` / `null`.
  - Integers and floats are decimal strings, so precision cannot differ between the two languages.
  - Timestamps and infinity carry only text and type.
  - The source text is there because the exporter reads thresholds as text; the converted value is there so the semantic layers need not write another copy of yaml.v3's type resolution.
- Semantic layers decide only by type and converted value, never by "yaml.v3's lenient conversion when decoding into a typed Go field" (for example the string `yes` decoded into a bool field becomes `true`).
- The output must be valid UTF-8.

**Unreadable files**: listed in `unreadable`, each entry `{file, kind, message}`; no content. `kind` is a fixed code, one for each case below (13 in all, given in parentheses), which readers classify by; `message` is the explanation for people. Each of these counts as unreadable, rather than taking what yaml.v3 read:

- A read failure (`read_failure`), not a regular file (`not_regular_file`), not valid UTF-8 (`invalid_utf8`), a syntax error (`syntax_error`).
- A top level that is not a mapping (`top_level_not_mapping`). An empty file counts as an empty mapping and is not included.
- Anything yaml.v3 decoding reports as an error (`decode_error`), including alias expansion beyond the limit, an explicit type that does not fit the text (`!!int abc`) and a merge key whose value is not a mapping (`<<: 1`). PyYAML reads some of these (for example `!!bool yes` as `True`); afterwards they are all unreadable.
- More than one document in a file (`multiple_documents`, separated by `---`). yaml.v3 reads only the first and silently ignores the rest; copying that would disable any policy after the `---`.
- A duplicate key within one mapping (`duplicate_key`), compared by key source text after alias expansion. yaml.v3 silently keeps the last value when the duplicate comes from an alias used as a key, so `parse-tree` checks this itself.
- A key resolved as null (`null_key`: `~:`, `null:`, an empty key), or a key that is not a scalar (`non_scalar_key`: `? [a]`, `? {c: d}`). Source-text output could not tell `~` from `"~"`; yaml.v3 also silently keeps the last of `~:` and `null:` in one mapping; and the generator and the exporter have handled null keys differently.
- A tag outside the allow-list (`tag_not_allowed`). Scalars accept only `!!str`, `!!int`, `!!float`, `!!bool`, `!!null`, `!!timestamp`; mappings and sequences accept only no tag or `!!map` / `!!seq`. `!foo`, `!!python/...`, `!!binary`, `!!set`, `!!omap`, `!!merge` are all unreadable: yaml.v3 reads several of them as plain strings, a different meaning from what the author or PyYAML meant.
- YAML's non-specific tag `!` (`non_specific_tag`, for example `! "x"`, `! "<<"`). It is outside the allow-list and unreadable at every step: reading `! "<<"` as a merge key or as a literal key could each silently add or drop policy. `parse-tree` detects `!` from the source text at each node's position, not from the node's tag, so it does not depend on the vendored yaml.v3 "non-specific tag" patch; after the patch is deleted in §7 step 4, `! "x"`, `! "<<"`, `! |`, `! 1`, `! {…}` and `!<!>` are still unreadable. The fixed corpus covers these spellings.
- A BOM (`bom_not_at_start`; U+FEFF, an invisible character that marks the encoding) anywhere but the start of the file. When the BOM straddles byte 512, yaml.v3 swallows the next character (measured: `domain_policies` read as `omain_policies`, silently disabling the whole policy); anywhere else it can leave an invisible character in a key or value. Rather than tell the cases apart, any BOM past the start of the file makes it unreadable.

**The envelope**: `schema` is `"da-guard.parse-tree/v1"`, alongside `da_guard_version`. A reader that meets any other `schema` value treats the whole document as unusable. Adding a field does not change the `schema` value; only a change to the shape a reader relies on does, the same convention as `served-values` / `effective`.

These "unreadable" rules are `parse-tree`'s own. How the exporter reads thresholds does not change, so the exporter may read a file the generator refuses (from step 4, when the generator reads `parse-tree`'s result; before step 4 the generator reads with PyYAML and does not necessarily refuse such a file, see §9); the generator and CI then block it rather than let it through.

**"Unreadable" is a closed list**: only the 13 cases listed above are unreadable. They are the forms the readers that remain after step 4 could read with different meanings; those readers are the exporter (yaml.v3 decoding into typed structures), `parse-tree`, the write tools that produce text with PyYAML (which must pass `parse-tree`'s single-file check before writing), and the portal's js-yaml previews (never a basis for letting anything through). Everything else is read per yaml.v3, including files today's generator refuses only because of PyYAML's limits, for example:

- An anchor name defined twice (`a: &x 1` and `b: &x 2`)
- A date that does not exist (`2001-13-45`)
- A flow value ending in a colon (`{b: 1:}`)
- `\'` inside a double-quoted string
- A block scalar header followed directly by `#` (`|#c`)
- TAB separators (a TAB after `a:`)

These files used to be refused whole; from step 4 they are read per yaml.v3. The migration audit command (§8) lists, file by file, every "used to be refused, now read as X" case, so the change is visible before the switch.

Why `parse-tree` does not refuse everything today's generator refuses: that would mean rewriting every PyYAML check in Go, exactly what this ADR rejects for two parsers; feeding random input to both sides and comparing kept finding new "only PyYAML refuses" classes. Measured on the riskiest example: yaml.v3 reads `{<<:, d: 1}` as a literal key `<<:` (with a null value), while PyYAML refuses the whole file. Handing yaml.v3's reading to today's generator, written as an equivalent quoted key (`"<<:": null`) to approximate the behaviour after step 4, leaves routing and policy outcomes unchanged: a tenant file still passes, with only an unknown-key warning and nothing merged in; inside `_domain_policy.yaml` the extra key is ignored and the policy still blocks the forbidden receiver.

The fixed corpus is `tests/shared/parse_tree_corpus.json`, built like the existing `merge_key_policy_corpus.json`: a Python test generates it and records the PyYAML version it was generated with. Each row stores the file text, today's generator verdict, and the category `parse-tree` should give (unreadable with which `kind`, or read); the test checks that category, and every `kind` in the closed list has at least one row. Today's generator verdict is frozen in the file as the record for the migration audit, because once the generator reads the JSON (§7 step 4), "what today's generator would say" can no longer be computed.

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

Output (excerpt, envelope fields omitted; measured by converting yaml.v3's nodes to JSON one by one, with `value` added by the rules above):

```json
{
  "t010.yaml": {
    "tenants": {
      "010": {
        "_routing": {
          "group_wait": {"text": "30s", "tag": "!!str", "value": "30s"},
          "receiver": {
            "api_url": {"text": "https://hooks.slack.com/x", "tag": "!!str", "value": "https://hooks.slack.com/x"},
            "send_resolved": {"text": "yes", "tag": "!!str", "value": "yes"},
            "type": {"text": "slack", "tag": "!!str", "value": "slack"}
          }
        }
      }
    }
  }
}
```

Today the generator reads keys with a loader that imitates yaml.v3, so `010` is already `"010"`; but `send_resolved: yes` is still read as the boolean `True`. Reading the JSON, it is the string `"yes"`: every Python tool sees exactly what the Go side read, and that loader is deleted too.

**How the Python side gets it**: through the existing "Python calls a Go binary and reads its JSON" mechanism (da-tools already reads da-guard's `served-values` / `effective` this way). The shared YAML-reading and file-selection libraries read this JSON instead; the tools no longer call a YAML parser on conf.d directly. Every place the generator uses a value switches to the JSON's type and value, not just `send_resolved` (for example `_severity_dedup: off` used to read as `False` and is now the string `"off"`).

**Python tools that write conf.d files are a stated exception**, both those that rewrite existing files (for example `patch_config`, `deprecate_rule`) and those that create new ones (for example `scaffold_tenant`, `init_project`, `onboard_platform`, `migrate_rule`). They read the original file and produce the text to write with PyYAML; the PyYAML loaders used for those two jobs are outside the deletion scope (§11). This ADR keeps that approach and adds limits:

- What to change, and whether to change it, is always decided from the JSON; PyYAML only produces the text.
- After producing the text and before writing it, the tool reads that text with `parse-tree`'s single-file mode, giving the path it will be written to:
  - The expected content is "the original file's parse result with this change applied"; for a new file, it is what the tool means to write.
  - Keys, structure and every scalar's source text, type and value across the whole file must match, or nothing is written.
  - Nothing is written either when the original file is unreadable or when no reader would read the target path.
- Known consequence: a file holding a spelling PyYAML rewrites on output (for example unquoted `1e3`, `0o17`, `0x1F`, which yaml.v3 reads as numbers) cannot be written at all until it is changed by hand to a quoted spelling. This errs on the safe side.

If Go later provides a rewrite command, this exception can be removed.

**Scope**: conf.d only. Rule packs, Kubernetes manifests and other YAML are out of scope for this ADR. The portal's in-browser tools (config diff, routing trace, YAML validator, playground and alert preview parsed with js-yaml, plus the hand-written line-by-line parsing in the config checker) are previews only and out of scope: their results are never the basis for letting a write or a CI run through, and their screens say that da-tools' result is authoritative.

### 3. How it runs (tenant-api)

**The pod gains a generator container, and tenant-api asks it before every write.**

```text
tenant-api pod
┌──────────────────────────────┐      ┌──────────────────────────────┐
│ tenant-api (Go)              │ ───▶ │ generator container           │
│ - receives the write request │ check│   (image: da-tools)           │
│ - writes or refuses by the   │ ◀─── │ - checks out files by commit  │
│   verdict                    │result│ - da-guard parse-tree parses  │
│ - reads the status endpoint  │      │ - generator judges semantics  │
│   for metrics                │      │                               │
└──────────────────────────────┘      └──────────────────────────────┘
          They talk only through a Unix socket in a shared directory
```

The diagram shows the state from §7 step 4 on. During steps 1 to 3 the generator in the container still reads files with its own PyYAML reader; parsing through `parse-tree` applies only from step 4.

The structure inside the container, conclusion first: a long-running process (a service process that stays up and keeps state across requests) checks files out and caches parse results, so a write does not re-parse the whole tree; each judgement goes to a new child process (copied from the long-running process, exiting after one judgement) that finishes within a memory limit and a judging deadline, one at a time. A child going wrong refuses only that one write; only the long-running process going wrong makes the whole container unavailable. Details follow.

- **Image**: the existing da-tools image, which already contains the generator and da-guard. The service uses only the Python standard library.
- **Communication**: HTTP/1.1 over a Unix socket; no network port. The socket lives in a shared scratch directory (emptyDir) inside the pod. Only processes that mount that directory and pass the socket file permissions can connect. The chart's existing human socket (for requests carrying a person's identity) works the same way.
- **Execution**: from step 1 on, the long-running process checks files out of git and caches each file's parse result across requests, so each write re-parses only the files it changes.
  - Why from step 1: re-parsing the whole tree each time spends most of the time reading files; measured, 1000 tenants on a single core with amtool already come close to 10 s, and 5000 tenants exceed the 20 s judging deadline (figures in "Cost").
  - **Judging happens in a child process**: for each request the long-running process forks a child that does the judging. Forking is how the operating system copies a process: the child starts with the same memory as the long-running process, so it inherits the cache as it stands. A batch is validated within that one child.
  - **One judgement at a time**: the long-running process lets only one child judge at a time; other requests queue, and time spent queuing counts toward the judging deadline (see "Deadlines" below).
  - The changed files are parsed in the child too, under the same memory limit and judging deadline; successful parse results are handed back to the long-running process and added to the cache. The long-running process never parses content that a write request brought in.
- **Cache**: a content-addressed cache, that is, a cache keyed by a hash computed from the content itself.
  - The key is (the SHA-256 of the file's content, the parse mode). It is computed from the file's bytes, so the same content always gives the same value, committed or not. Not git's blob SHA: in a SHA-1 repository a blob SHA can be deliberately collided, two different contents would share one key, and the parse of a refused content would be used to judge the other — a way around the judgement. The parse mode is which parser and which output form: in steps 1–3 the result of decoding with PyYAML, from step 4 on `parse-tree`'s output for a single file. The same content read by different parsers gives different results, so the parse mode is part of the key.
  - Why it never serves stale content: the key is computed from the content itself, so changed content gets a different SHA and can never pick up an old result; no expiry by time or by commit is needed. The parser version does not change within one process, so the result for a given key does not change either.
  - Only the decoded content structure is cached. Verdicts that depend on the file's path, such as which files are selected and which are unreadable, are not cached and are recomputed on every judgement.
  - The parse results a child hands back to the long-running process must be as they were before the generator modified anything (a deep copy or the serialized result). Whatever the child changes in the cache it inherited disappears when the child exits and does not affect the next judgement.
  - The deployment policy file is not in git; it is not in this cache and is re-read on every judgement.
  - **Eviction**: blobs that appear in the tree of the latest base commit are always kept. Those not in that tree (for example content brought in by refused writes) are capped at 32 MiB in total, measured as the byte size of the serialized results the children hand back; when the cap is reached the least recently used entry goes first (LRU). A cache that only grows would make the long-running process's memory grow without bound.
- **Deadlines**: a request has a single overall deadline, 30 s from when tenant-api receives it, which is the HTTP server's write timeout (`--write-timeout`, 30 s by default; there is also a fixed 30 s request-timeout middleware). Waiting for the write lock, calling the generator container, revalidating and committing all fit inside those 30 s.
  - Each time tenant-api calls the generator container it passes the time remaining. The container's judging deadline is the smaller of 20 s and the remaining time minus 5 s; those 5 s are for tenant-api to check the result and commit. This is like gRPC's deadline propagation: the caller passes its remaining deadline downstream, so downstream never keeps working after the caller has given up.
  - The judging deadline starts when the long-running process receives the request, so time spent queuing counts. When it passes, the long-running process answers "refuse this write" whether the request is still queued or already being judged; a child already judging is ended along with it (see the next point). The remaining time is passed as a duration ("how much is left"), so it does not depend on the two containers' clocks agreeing; the judging deadline falls 5 seconds before tenant-api's remaining time runs out, and those 5 seconds cover sending the answer back as well as the check and commit described in the previous point. So as long as the resident process itself is healthy, tenant-api gets this write's result within its own deadline and never treats the container as unavailable just because it stopped waiting.
  - When the remaining time is not enough for one more revalidation (§4, "What was validated is exactly what is written"), tenant-api returns a retryable refusal directly, of the same kind as "refuse this write", not 503.
  - The child runs in its own process group via `setsid` (a process group is a set of processes, the child and everything it starts, sharing one group id). When the judging deadline passes the whole group gets `killpg`, so amtool, da-guard and any other descendants end too, leaving nothing behind.
- **Resources**: limits at two levels.
  - The container's request equals its limit: 512Mi of memory, 1 CPU.
  - Each judging child additionally gets a data-segment limit (`RLIMIT_DATA`, 192Mi). `RLIMIT_DATA` is one of Linux's per-process memory limits; since Linux 4.7 it counts the process's private, writable memory, that is, memory data can actually be written to, and allocations beyond it fail. The cache the child inherits at fork counts too, so the cache's size counts against those 192Mi.
  - The limit is inherited by the programs the child starts: amtool and da-guard each get the same 192Mi. Measured under this limit, the generator plus amtool still passes over a 5000-tenant tree, and `da-guard parse-tree` (built with `CGO_ENABLED=0`, as in the image) reads the same tree fine; Python asking for 500Mi at once gets a MemoryError; a Go program that keeps allocating ends with out of memory at the limit.
  - Why the total fits in 512Mi: with one judgement at a time, the worst case is the child and one Go program it started both using their full limit, plus the long-running process: about 192 + 192 + 80, roughly 464Mi, below 512Mi. Measured at 5000 tenants, the generator peaks at about 117 MiB and amtool checking the generated 2.3 MB config at about 104 MiB, both within 192Mi. The long-running process's 80Mi is an estimate: a Python process holding the parse results of the latest tree's 5000 files measured about 36 MiB, plus the 32 MiB cap on the refused-content cache, the rest being git and other overhead.
  - **Capacity limit**: the latest tree's cache has no cap and grows with the number of tenants; children inherit it at fork, so it counts against their 192Mi. Once the cache plus one judgement's peak exceeds 192Mi, every judgement is refused, and the child limit and the container limit must be raised together. To see it coming, the long-running process exposes two metrics, the cache size and the peak memory of a single judgement, with a warning alert as they approach the child limit; the threshold is left to the implementation.
  - Why not the address-space limit (`RLIMIT_AS`): amtool and da-guard are Go programs, and the Go runtime reserves a large range of virtual addresses as soon as it starts (addresses only, no writable memory yet, so not counted by `RLIMIT_DATA`). Measured, amtool does not start with an `RLIMIT_AS` below 768Mi (384Mi to 700Mi all tried), far beyond the child's memory budget.
  - A cap on the total output of `parse-tree` over the whole tree is not decided in step 1; it is set in step 4 (when the generator switches to reading the JSON).
- **Refusing one write versus the container being unavailable**: only when the long-running process responds, and the response states this write's result (passed, has findings, or refused because the judging deadline passed, the memory limit was exceeded or the output was incomplete), is it "refuse this write". The container is unavailable in exactly three cases: the long-running process fails or does not respond, amtool is missing (see "amtool is required" below), or the generator in the container is older than a policy requires (§6); all three return 503 (§5).
  - Refusing one write: the HTTP status is not 503, and the error code differs from `VALIDATOR_UNAVAILABLE`; its name is left to the implementation. Refusals caused by the deadline, the memory limit or incomplete output are counted in a metric. The container stays available; if these counted as the container being unavailable, a single write could make every later write get 503.
  - Container unavailable: `VALIDATOR_UNAVAILABLE`, 503 (§5).
- **amtool is required**: amtool is Alertmanager's config-checking tool. The generator's validation mode only prints a notice and passes when it is missing; the generator container does not accept that: before every judgement it checks that amtool is present, and counts itself unavailable if not. Otherwise the generator would exit 0 with zero findings, which looks like a pass.

**Getting the files: the generator container reads git objects only, never the working directory.**

- It mounts the same repo tenant-api uses, read-only, and checks out files at a given commit (`git ls-tree` plus `git cat-file`).
- Not the working directory: PR mode (opening a PR per write instead of committing directly) switches branches in place, so a direct read can see a half-switched state.
- Not `git archive`: it applies the export rules in `.gitattributes`, and if a customer marks the policy file as not exported, the generator would not see it.
- **Regular files only**: only tree entries with mode 100644 or 100755 are written into the tree being judged. Every other entry is judged `not_regular_file` (that item of §2's "unreadable" list), including a symlink (mode 120000) and a gitlink (mode 160000, a submodule); it is a finding that refuses the write.
  - Why: read from git objects, a symlink is only its link text, not the file it points to, and a gitlink has no content. Judging the link text makes the result depend on what the text happens to look like, for example text that happens to read as a mapping.
  - Example: `t1.yaml` is a symlink to `real.yaml` in the repo. The write is refused with the finding `tenant_file_unreadable`, reason "is not a regular file (not_regular_file): git tree mode 120000, a symlink"; the fix is to replace it with a regular file with the same content. tenant-api already writes a regular file when it writes that path, so a write of that path itself is not affected.
  - It is classified as any other file that cannot be read, by its name: a domain policy file name (`_domain_policy.yaml` / `.yml`) is reported as `domain_policy_unusable`, any other as `tenant_file_unreadable`; both refuse the write.
  - Exception: an entry with a path component starting with `.` is not judged, because every reader skips hidden entries.
  - Names do not matter: apart from that exception, every entry that is not a regular file refuses the write, including one that is not a config file name, such as `README.md`. This is stricter than the readers that read from the file system: they pick config files by name, and skip a symlink that is not a config file name without a word. This is on purpose: the judgement follows one rule, the git mode, and does not have to stay in line with the list of which names count as config files; and there is one fix, replace it with a regular file or remove it.
  - Such an entry still occupies its path. Writes are applied one by one in the order of the request, and each sees the tree the writes before it left:
    - A write below it (at any depth: with `l` a symlink, `l/x.yaml` or `l/a/b.yaml`) is a 400, as one below a regular file is.
    - Once a write in the request writes or deletes its path, it is no longer judged `not_regular_file` (tenant-api writes a regular file there, or nothing is left there). Only after a delete may later writes go below it: deleting `l` and then writing `l/x.yaml` in one batch passes, and the other order is a 400; writing `l` as a regular file and then writing `l/x.yaml` is a 400 too, as a write below any regular file is.
    - The directory holding it is a directory: with `d/s.yaml` a symlink in the tree, writing `d` as a file is a 400, as it is when `d/s.yaml` is a regular file.
  - The reads the generator container makes to fill its cache ahead of requests (at startup, and after it sees a new base commit) read only the files written to disk in the tree being judged and produce no findings, so these entries play no part in them.
  - Readers that read from the file system (the exporter, `da-guard parse-tree` reading a directory, customer CI) still follow file symlinks: after a Kubernetes ConfigMap is mounted, every key is a symlink, so they cannot be forbidden. So for the same tree, customer CI can be green while tenant-api refuses writes.
- **Both containers must run as the same uid** (the Linux user id a process runs as): git refuses to read a repo owned by another uid (measured: reading with `-C` as a different uid reports dubious ownership). The chart pins this with a test.
- **The repo disables git's automatic garbage collection (`gc.auto=0`)**. Measured: as the same uid, one side ran 15 rounds of commit plus `repack -a -d` plus `gc --prune=now` while the other read a fixed commit 1376 times, with 0 failures; a read-only mount reads fine.
- **On a read failure**: retry once, then refuse.
- **git is required**: deployments without git are no longer supported.

### 4. How writes are validated

**After a write, the generator checks the whole tree; the write goes ahead only if everything passes.**

- **Criterion**: the generator container replaces the files being written with their new content and runs `--validate --strict` on the whole resulting tree. Exit code 0 means write; anything else means refuse.
  - It uses the same base commit, the same deployment policy file, the generator version in the container, and amtool must be present.
  - The criterion is the generator's own result, so tenant-api is never looser than the generator in its container.
- **Which writes are validated**: every write that changes file content. Whether to validate is not decided by Go guessing "does this touch routing".
- **Error responses**: list the findings the generator reports (each problem it reports). Those related to the written files come first, marked with which existed before the write and whether they come from platform defaults, a routing profile or the tenant itself. This affects only the message, never the verdict.
- **Batches**: one request, one commit, all or nothing. The batch is written only if the whole tree passes with the batch applied; otherwise the whole batch is refused, and the response lists the related findings item by item. Intermediate states are not validated and never reach the repo. Synchronous and asynchronous batches follow the same rule; an asynchronous batch is judged when it runs.
  - Each batch is applied, judged and committed in a separate temporary git worktree (another working directory checked out from the same repo). After the commit succeeds, the branch ref is advanced first, then the serving working directory is updated. Before advancing, the paths this batch will touch are recorded; if advancing is interrupted (for example, the process crashes), those paths are reset to the branch's content at startup and before the next write. Only this batch's paths are reset; other uncommitted edits in the working directory are left alone.
  - What is guaranteed: readers never see content that was never committed; if it fails or the process crashes midway, the half-applied batch stays in the temporary worktree. While the working directory is being updated, a reader may briefly see a mix of old and new content, but both are committed. This follows the same principle as §3, where the generator container reads only git objects, never the working directory.
  - Before this change, batches are judged item by item: the violating items are marked as errors and the rest are written. Switching to all-or-nothing is a breaking change released on its own before step 1: the prerequisite that lets git writes commit several files at once (§7) already switches to all-or-nothing, still judged by Go's existing checks at that point; step 1 only switches the criterion to the generator, and the all-or-nothing semantics do not change.
- **Merging batch patches**: Go still reads the file, applies the patch and writes it back; the merged content is always validated.
  - After the merge an invariant must hold, or nothing is written: the original file and the merge's result are both parsed into yaml.v3 node trees, and outside what this edit replaces every node must be written the same: kind, style (flow or block, plain or which quotes), the tag as written (an explicit tag's text, or none written), a scalar's text, the anchor name, and the node an alias points to (compared by content, not only by name). The documents must be as many, and the file must define the same anchors in the same order. The same characters mean the same to every reader, so the rule does not depend on any reader. The edited keys are also decoded and checked: a key set holds its new value and a key removed is gone, because a `<<` merge key can still bring in a removed key. Comments and positions are not compared.
    - Any difference refuses the write with `TENANT_CONFIG_NOT_LOADABLE` (409 for `PUT .../custom-alerts`; for a batch, that operation's code and 422 for the batch) and `config_error` `not_round_trippable`; the message names the first difference, as a path only inside the tenant's own block and as "another place" anywhere else, which may be a tenant the caller cannot read.
    - Example: `k: [1:20]` in another tenant's block. yaml.v3 re-emits it as `['1:20']`; PyYAML reads the original as the integer 80 and the new file as a string. The node's style changed from plain to single-quoted, so the write is refused. The same invariant refuses a block scalar whose leading blank line is rewritten, a tenant block shared through an alias that would change along with it, a repeated anchor re-pointing an alias, and a value a `<<` still supplies after the explicit key is removed.
    - Every value the merge writes is double-quoted (a batch patch value is a string; so is every string in the `_custom_alerts` Go generates for custom-alerts), whatever it holds, so no reader takes it for a time, a number or a boolean. The one exception is a string with a line break in `_custom_alerts`: it is written as a literal block (`|`, also a string to every reader) so a diff shows its lines, provided that block reads back as the same string (one opening with a line break or a tab does not); otherwise it is double-quoted too. Before re-emitting, the implicit tags yaml.v3 filled in are cleared; otherwise `<<` comes back as `!!merge <<`.
    - Known limit: a writing that differs only outside the node tree is not caught. The measured example is a TAB right after `k:`: PyYAML cannot read the original, yaml.v3 reads an empty value, and after re-emission both can read it, so the unreadable file is in effect fixed. The differential test pins this case.
    - When decoding fails, the text returned to the caller is fixed: what failed and on which line. The parser's own text goes to the server log only, because a file can hold several tenants and that text quotes another tenant's value.
  - What validation cannot catch is a rewrite with no violation whose meaning Go changed; an offline differential test guards that: one side has Go merge and the generator read the result, the other has the generator read the base and apply the same patch; both must match, including the other tenants in the same file.
- **What was validated is exactly what is written**: the container returns the "file paths, content hashes, base commit, deployment policy file content hash" it validated. tenant-api checks them under the write lock; it mounts the same deployment policy file read-only and computes that hash itself. On a mismatch it revalidates once, and refuses if they still differ; the bytes written must be exactly those validated.
- **PR mode validates the branch point**: what is validated is the PR branch's base commit plus this write. The result after the PR merges into the main branch is outside tenant-api's verdict; the customer's CI or a merge queue guards it.
- **How anomalies are handled**: which cases refuse only that write and which make the container unavailable with 503 is set out in §3, "Refusing one write versus the container being unavailable".

**Cost (a breaking change)**:

- While anything anywhere in the tree would be blocked, every write through tenant-api is refused until it is fixed.
- All-or-nothing batches: if one item fails, nothing in the batch is written; fix that item and resend the whole batch (§4).
- How to fix:
  - When the problem is in one tenant file and fixing that file makes the whole tree pass, it can be fixed through tenant-api.
  - Everything else goes through git, for example platform files, files in subdirectories, or several tenants violating at once (common after the platform tightens a policy). Problems that appear only after a PR-mode merge belong here too.
  - When the customer's CI is green but tenant-api judges the whole tree red: rerun locally with the container's generator version (read from the §6 metric) and the deployment policy file, find the findings, and fix through git.
- Why this is acceptable: when the customer's CI uses the same da-tools version and passes the same deployment policy file, a change that turns the whole tree red cannot reach the main branch; when the whole tree is red, the customer's CI is already red. This amounts to "the main branch always stays green", the same as a merge queue (a queue that runs tests on the latest main branch before merging). If either condition is missing, the customer's CI can be green while tenant-api's whole tree is red; the last fix path above covers that.

### 5. Policy files and policy status

**The deployment's policy file is mounted into the generator container; when policy is unavailable, writes are refused.**

- **The deployment's policy file** (`--policy`): its `allowed_domains` restrict webhook URL domains. The chart gains an optional value that mounts this file into the generator container, and it is passed along when judging; the tenant-api container mounts the same file read-only to check its hash (§4). Reason: this is an SSRF control (stopping the service from being used to send requests into the internal network on an attacker's behalf) and belongs outside the tenant repo; someone with write access should not be able to change it. The customer CI templates `da-tools init` generates can optionally bring in the same file, so the customer's CI and tenant-api judge against the same file: GitHub reads the repository or organization variable `DA_TOOLS_POLICY` (its value is the policy's YAML content), GitLab reads a File-type CI variable of the same name (its value is the path of the file the runner writes); when set, `validate-config` and `generate-routes` pass `--policy`, and when unset the pipeline prints a note and does not fail. The CI side is not a security control, since anyone with write access to the repository can edit the workflow; what it buys is seeing tenant-api's answer early, on the pull request. The pre-commit hooks do not pass it, because they can mount only the repository itself. Setup: [GitOps CI/CD Integration Guide §2.5](../scenarios/gitops-ci-integration.en.md#25-bringing-in-the-deployment-policy-file-da_tools_policy).
- **Status endpoint**: the generator container reports whether the deployment policy file is usable, and tenant-api reads it to produce `tenant_api_policy_available`. This metric reflects only the deployment policy file and the container; an error in a policy file inside the repo does not set it to 0, and such writes are refused with findings. Alerts read this metric, so it keeps its name; it has no labels today.
  - When the status endpoint cannot be reached (container unreachable), the metric is 0, so the alert still fires.
  - Its description counts "keeping the last usable content" as usable; once tenant-api asks the generator container, old content is no longer kept, so the description changes then.
- **No escape hatch**: when the deployment policy file or the generator container is unavailable, every write that changes file content is refused (thresholds-only writes included) with 503.
  - 503 is used only for these two cases. An error in a policy file inside the repo (platform defaults, a routing profile) is a finding the generator reports and is refused per §4, not with 503.
  - When the deployment policy file is unavailable the error code is `POLICY_UNAVAILABLE`. When the generator container is unavailable (the long-running process failing or not responding, amtool missing, older than a policy requires) the error code is `VALIDATOR_UNAVAILABLE`, with which of these it is in the message. A judging child timing out or exceeding its memory limit does not make the container unavailable (§3).
  - Today tenant-api's scope is narrower: only in direct write mode, and only when a policy file exists but none of its content is usable; a single PUT always gets it; a synchronous batch gets 503 for the whole request if any item touches routing; an asynchronous batch re-judges each item when it runs. The new rule does not depend on the mode.
  - `--policy-unavailable-open` and the counter `tenant_api_policy_unavailable_open_total` are removed. The alert `TenantApiPolicyUnavailable` stays (it reads the availability metric), but its description mentions the escape hatch and the counter and changes with them.
- **No resolved config JSON is produced**: no code needs to read one today.

### 6. Versions

Which da-tools version a customer's CI runs is independent of tenant-api, so "exactly matching the customer's CI" is not achievable. What is guaranteed is: **tenant-api is never looser than the generator in its own container**. da-guard and the generator ship in the same da-tools image, so their versions are decided together.

When the two sides run different versions, a refusal on either side keeps the config out of Alertmanager:
- If the customer's CI version is stricter, a write tenant-api accepted is blocked in CI. It is blocked one step later, never let through.
- If the version in tenant-api's container is stricter, tenant-api refuses writes the CI would have accepted.

Neither case lets a violating config through; the cost is that the two sides can disagree. To narrow the version gap:

- tenant-api's chart pins a default da-tools version, and CI runs compatibility tests against that pair.
- The config generated by `da-tools init` pins a version instead of `latest`.
- A policy file can declare "requires at least generator version X"; a container older than that refuses every write that changes file content; the fix is for the operator to raise the da-tools version set in the chart. This is a new key, read only by the generator; it must be added no later than the step where tenant-api starts asking the generator container (§7 step 1), or older generators will not recognise it.
- tenant-api exposes the generator and da-guard versions as metrics.

### 7. Merge in order on main

**The five steps below merge into main in order, and every step can be released once merged.** There is no side-by-side period, no switch, and no divergence catalog (a list recording each config file Go and the generator judge differently). Why not do it all in one release: see "Considered and rejected".

- **Step 0: ship first, no behaviour change.** `parse-tree` (single-file mode included) and its fixed corpus (§2); the migration audit command (§8); move the small utilities unrelated to parsing (for example the tenant id check, finding which directory level a file sits at, recognising policy file names) out of the packages to be deleted.
- **Step 1: tenant-api's writes ask the generator container.** The same step deletes all of tenant-api's Go-side routing and policy judging (§11), including the imitation that decides whether `_routing` is a string that turns routing off. Checks that are not routing judging stay: the guard against writing back masked-credential placeholders, the yaml.v3 check that the exporter can read the written content, and the value checks on reserved keys such as thresholds. The last two keep tenant-api from letting through files PyYAML reads but yaml.v3 does not (for example `"x\/y"`); duplicate keys the Go imitation package used to block are now blocked by the generator's own duplicate-key refusal. The same step also removes `--policy-unavailable-open` and deletes the flat key `_routing_receiver_type` (the old single-level way to set the receiver type); writing it now returns 400.
- **Step 2: da-guard reports "not judged"** (§9); `guard-defaults-impact.yml` also runs the generator.
- **Step 3: delete the Go code that imitates PyYAML and imitates the generator's semantics** (§11), along with the divergence catalog and the expiry check in `make pre-tag`. The vendored yaml.v3 "non-specific tag" patch (the change that keeps YAML's `!` marker on the node) stays in this step.
- **Step 4: the generator and the Python tools read the parse-result JSON.** Delete the Python loaders that imitate yaml.v3 and the "non-specific tag" patch; the tools that write conf.d add the pre-write check (§2). The shared Python libraries and the tools (the generator included, which uses the shared libraries directly) switch together in this step; switching the shared libraries first would leave the generator reading half JSON, half PyYAML.

**Guarantee**: from step 3 on, no Go code produces routing or policy verdicts (findings); what the exporter keeps is only the lookup of whether a receiver type exists. Between steps 1 and 2 there are two judges side by side, Go (da-guard) and the generator, no looser than today. The generator only reads the yaml.v3 way from step 4, when the Go code imitating PyYAML is already deleted and the "non-specific tag" patch is deleted in the same step; so whichever commit is released, there is no release where the generator already reads with yaml.v3 while Go still imitates PyYAML.

**Why `parse-tree` is step 0**: part of what blocks duplicate keys, more than one document and tags outside the allow-list today is the Go imitation code. Before step 3 deletes it, `parse-tree` must already treat these as unreadable (§11).

**Why the "non-specific tag" patch waits until step 4**: the exporter relies on it today to read the same way PyYAML does.

- `! "<<"`, marked with `!`: the exporter's merge-key check also counts a `"<<"` tagged `!`; PyYAML merges it too.
- A custom-alert SLO `objective: ! "99.9"`: the exporter refuses it because its tag is `!`; PyYAML reads it as a float, and the Python side refuses it as well because the objective is not a string.

Deleting the patch before the generator reads with yaml.v3 would leave a released version where the exporter and the generator read differently, and for the SLO objective the exporter would be looser than the generator and CI.

**What must be done before step 1**:

- The config `da-tools init` generates pins the da-tools version.
- tenant-api's git writes support committing several files at once, and batches become all-or-nothing at the same time, released on their own (§4).
- The generator container (validation endpoint, status endpoint); the Helm chart adds the container, socket, policy-file mount and resources.
- The checks only Go's da-guard has move into the generator (§9).

**No falling back to Go when the container is unavailable**: tenant-api's Go judging is deleted in step 1; and anyone who can send writes can affect whether the container is available (for example by sending many requests that need whole-tree recomputation), so falling back to a looser judge would let the requester pick the judge.

The customers' conf.d trees are not available, so validation before each step merges covers only the owner's tree. The migration audit command (§8) ships in step 0, so customers can see the impact on their own tree first.

### 8. Migration: values whose reading changes

Once yaml.v3 parses the YAML, any value the two parsers read differently changes for the generator and the Python tools. The most common are unquoted `yes` / `no` / `on` / `off` (previously booleans, now strings); a key tagged `!!merge` (previously merged) makes the whole file unreadable.

- da-tools provides a one-off migration audit command: it reads the customer's tree with both parsers and lists four kinds of result:
  - Values read differently: file, line and suggested spelling (for example rewriting a boolean as `true` / `false`).
  - Keys read differently.
  - Files that become unreadable as a whole (for example `\/` inside a quoted string, more than one document, a null key, a tag outside the allow-list such as `!!merge`, a BOM inside the file).
  - Files that used to be refused whole and will be read: what yaml.v3 reads each one as (the closed list in §2).

  This command and the writing tools of §2 are the only places in this ADR that still read or write conf.d with PyYAML; the audit command only lists differences and judges nothing.
- The release notes list the affected spellings and the migration steps.
- The exporter already uses yaml.v3, but once the vendored yaml.v3's "non-specific tag" patch is deleted (§7 step 4), how it reads thresholds and custom alerts changes in two places, both listed by the migration audit:
  - A quoted string marked with `!`, `! "<<"`, is no longer taken as a merge key and goes back to yaml.v3's own reading (a literal key).
  - A custom-alert SLO `objective: ! "99.9"` used to be refused because its tag was `!`; afterwards its tag is `!!str` and it is accepted. This one is looser.
  - `parse-tree` still treats both spellings as unreadable (the non-specific tag in §2), so the generator and CI block any file containing them; this is the "exporter reads it, generator refuses" case §2 describes, and nothing is let through.

### 9. da-guard

da-guard gains `parse-tree` and stops judging routing and policy; the generator's `--validate --strict` judges them. Threshold checks are unchanged.

- **When da-guard runs alone** (for example the standalone binary without Python):
  - The report (text and JSON) states that routing and policy were "not judged", and returns exit code 4, so "not judged" and "judged, no problem" can be told apart.
  - It returns 4 only when the tree contains content that only the generator can judge: routing, policy, or `_` keys that only the generator reads or, misspelled, only the generator reports (how they are judged: [ADR-038](./038-unread-keys-judged-by-readers.en.md)). A tree without any still returns 0.
  - When several exit codes apply, the precedence is 3 > 1 > 4 (3 is files that cannot be read, 1 is error-level findings; 3 over 1 is unchanged from today).
  - The one definition of what exit 3 covers is [the guard section of the CLI reference](../cli-reference.en.md#guard): files the exporter drops whole when it loads, files that fail to decode while merging (the `_defaults.yaml` chain or a tenant file), and files, directories or paths that cannot be read. A file `parse-tree` treats as unreadable while the exporter reads it is not reported with exit 3; it is listed as "not judged" evidence (`generator_unreadable`, exit 4) and left to the generator. Before step 4 the generator (PyYAML) does not necessarily refuse such a file: the fixed corpus has rows it reads, such as a BOM in the middle of the file or a tag like `!!binary`; others it refuses whole, for example for a repeated key. From step 4 the generator reads `parse-tree`'s JSON, and refuses every such file.
  - When step 4 is done, re-evaluate whether exit 4 still needs to exist: da-guard's condition for 4 will still be a prediction of whether the generator judges the tree; if almost every real tree returns 4, the condition carries no information.
  - No flag lets CI declare "routing already judged by the generator": the customer CI templates `da-tools init` generates already run `generate-routes --validate --strict` and never run da-guard alone.
  - Migration: wherever CI ran da-guard, add a `da-tools generate-routes --validate --strict` step.
  - `--required-fields _routing.*` errors out instead of silently doing nothing.
- **Before deleting Go checks**, move the checks only Go has into the generator, add them to both sides' comparison table, and prove the tests go red without them. The "only Go has" list is computed by code, not maintained by hand. A check that was a warning and becomes an error after the move is noted as "stricter" in the changelog.
- `guard-defaults-impact.yml` (the CI that checks the impact of a PR changing defaults) also runs the generator.

### 10. tenant-api read endpoints

- No GET endpoint returns generator-rendered routing today, so there is nothing to migrate.
- `GET /tenants/{id}/routing` is added only when someone needs rendered routing, and its content is produced by the generator container.
- The `_routing` in `/effective` responses is the raw key as the exporter merged it, not rendered routing. The API description will say so.

### 11. What gets deleted

Decided by "is anyone still using it". CI already has a check: it lists the users of the relevant packages and goes red when they differ from the declaration table.

| Item | When | Action |
|---|---|---|
| Small utilities unrelated to parsing (for example the tenant id check, finding which directory level a file sits at) | step 0 | move to another package |
| The rest of tenant-api's Go-side routing and policy judging: the checks on a write's routing patch and receivers, judging a tenant's routing against policy, the old behaviour of treating an unreadable file as empty | step 1 | delete |
| Go code that imitates PyYAML's reading: the PyYAML-decoding imitation package, the function imitating PyYAML's type resolution, the "spaces only" option the policy decoder turns on | step 3 (tenant-api stops using it in step 1, da-guard in step 2) | delete |
| Go code that imitates the generator's semantics (the Go copy of routing and policy judging, including deciding whether `_routing` is a string that turns routing off) | same | delete |
| The Go copy of the receiver contract (which receiver types exist, which fields each requires) | the judging part: step 1 for tenant-api, step 3 for the rest | delete the judging part; the exporter only uses it to look up whether a receiver type exists, and that part stays |
| The vendored yaml.v3 "spaces only" patch (refusing TAB indentation; only the policy decoder above turns it on) | step 3 | delete |
| The vendored yaml.v3 "non-specific tag" patch (YAML's `!` marker, making a quoted string typed by the unquoted rules) | step 4 | delete; besides the imitation packages, the exporter (`pkg/config`) relies on it too, and deleting it changes two of the exporter's readings (§8), recorded as a breaking change. Why it cannot go earlier: see §7 |
| The vendored yaml.v3 performance change the exporter also uses (duplicate-key checking of large mappings via a hash table) | — | keep |
| The divergence catalog, its tests and the expiry check in `make pre-tag` | step 3 | delete |
| Python loaders that imitate yaml.v3, the Python copy of the file-selection rules | step 4 | delete; the loaders the conf.d-writing tools use to read originals and produce text are not included (§2) |

Step numbers are those of §7; the code for each item is in the appendix. CI's declaration table records when each package and patch may be deleted; the PR of each step that deletes code updates it.

Duplicate keys, more than one document, tags outside the allow-list: today the generator's loader and the Go imitation package block these (some Python tools deliberately read only the first document). Before step 3 deletes the Go checks, `parse-tree` must already treat these as unreadable (§2), confirmed by the fixed corpus; that is why `parse-tree` ships in step 0.

## When things fail

| Situation | Behaviour |
|---|---|
| The generator's exit code on the whole tree after the write is not 0 | Refused; the error response lists the findings, marking those that existed before the write and their source |
| The generator container is unavailable: the long-running process fails or does not respond, or amtool is missing | Every write that changes file content gets 503, error code `VALIDATOR_UNAVAILABLE`; `tenant_api_policy_available` is 0 (§5) |
| The judging deadline passes (including while queued), or a child exceeds its memory limit or produces incomplete output, or the remaining time is not enough to revalidate | Only that write is refused, no 503 (the line is drawn in §3) |
| The deployment policy file is unavailable | Every write that changes file content gets 503, error code `POLICY_UNAVAILABLE` |
| A policy file inside the repo has an error | The generator reports a finding; refused as in the first row, not with 503 |
| `parse-tree` cannot read a file (from §7 step 4) | The file is listed under unreadable files, the generator's check does not pass, and the write is refused |
| Text produced by a Python tool that writes conf.d reads back through `parse-tree` differently from what was expected | Nothing is written, and the differing value is reported |
| A reader meets an unknown `schema` value | The whole document is treated as unusable |
| The generator in the container is older than a policy requires | Every write that changes file content gets 503, error code `VALIDATOR_UNAVAILABLE`, until the operator raises the chart's da-tools version |
| No da-guard locally and no Go to build it | Python tools that read conf.d and the pre-commit hooks fail with the reason; they never fall back to parsing on their own. The affected hooks and the CI jobs without Go are listed by code in the implementation PR; those CI jobs switch to the da-tools image or install Go |

## How it is accepted

- **No second YAML parser**: a CI check lists every YAML loader call site on the Python side and allows only the parse-result JSON; the only exceptions are the migration audit command and the writing tools of §2, fixed in the check's allow-list.
- **"Unreadable" is a closed list**: each row of the fixed corpus `tests/shared/parse_tree_corpus.json` carries the category `parse-tree` should give, and the test checks every row; every `kind` in the closed list has at least one row (§2).
- **The fixed corpus** also includes the existing `merge_key_policy_corpus.json`, plus the samples from:
  - [#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759): the remaining gaps for policy files.
  - [#2700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2700): `!!merge` keys in tenant files.
  - [#2713](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2713): an unparseable value inside an overridden merge source.
  - [#2674](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2674): a self-referencing YAML alias.
- **The offline differential test for batch patch merging** (§4).
- **da-guard's "not judged"**: exit code 4 and the report content are tested, including no 4 when there is no content only the generator can judge, and the precedence when 3 or 1 also applies.
- **The generator container's refusal conditions**: amtool missing, validated and to-be-written content still differing after one revalidation, and a generator older than a policy requires each have a test, and each ends in a refusal. A judging child exceeding its memory limit, the judging deadline passing during judging, and it passing while queued each have a test too: only that one write is refused, no 503, the next write is judged as usual, and no descendant process (amtool, da-guard and so on) is left behind after a timeout; a retryable refusal is returned when the remaining time is not enough to revalidate; and after an interrupted advance, this batch's paths are reset to the branch's content.
- In CI, run against the version pair the chart pins.

## Cost

Semantic layer (the generator, measured 2026-10-09):

| Item | Measurement |
|---|---|
| Generator over 1000 tenants | 1.72 s, 38 MB; parsing is 1.06 s of that, and each file is parsed twice (a cache can save this) |
| Parsing one file | 0.47 ms |
| Python startup | 0.11 s, 25 MB |
| Checking 1000 files out of git | 0.21 s, 12 MB |
| Image size (compressed, amd64) | da-tools 36.9 MB; tenant-api 14.0 MB |

Generator container (measured 2026-10-10, a 1000-tenant tree): about 3.1 s with a new process per request; about 1.5–1.8 s in the same process with a parse cache.

Scale of write judging (measured 2026-10-11; a synthetic tree, one file per tenant, each with `_routing`, thresholds and `_metadata`; not the tree used for the rows above, so the numbers are not directly comparable):

| Item | Measurement |
|---|---|
| Generator `--validate --strict`, 1000 tenants (4 cores, no CPU limit, no amtool) | about 8–9 s |
| Same, 5000 tenants | about 42 s |
| Same, 1000 tenants, single core, with amtool | 9.4–9.8 s |
| Share spent reading files with PyYAML | about three quarters |
| `da-guard parse-tree` reading the same 1000-tenant tree | under 1 s |
| Keeping the parse results of 5000 files in the process | about 25 MB more resident memory |
| 5000 tenants, generator peak memory | about 117 MiB |
| 5000 tenants, amtool checking the generated 2.3 MB config | about 104 MiB |

This is the basis for §3 using a long-running process with a parse cache from step 1.

Syntax layer (`parse-tree`, measured 2026-10-11; 1000 files with one tenant each, about 20 lines per file including `_routing`, `_metadata` and a scheduled threshold):

| Item | Measurement |
|---|---|
| yaml.v3 parsing each file into nodes and converting to JSON (an experimental program standing in for the not-yet-implemented `parse-tree`; it outputs only text and type, with no `value` and no merge expansion) | about 0.11 s, 29 MB allocated in total, 795 KB of output |
| Python reading that JSON | 0.024 s |
| For comparison: PyYAML reading the same 1000 files | 1.22 s |

After switching to the JSON, the Python side's parsing cost falls to roughly a tenth or less of what it was; with `value` and expansion the output grows somewhat.

## Considered and rejected

The options whose reasons are already given in the body (keep both sides imitating each other, check out with `git archive`, fall back to Go when the container is down) are not repeated.

| Option | Why rejected |
|---|---|
| Restrict conf.d to what "the two parsers are guaranteed to read the same way" | Measured, it does not work: `\/` inside a quoted string and a TAB after a colon are read differently even with the value quoted and no advanced feature used. Such a subset can only be sampled, never defined |
| On a write, look only at the findings that belong to this write (per-finding attribution) | The way the generator reports findings was not designed to assign responsibility: its validation short-circuits, so after an earlier error the assembly and amtool never run; a run-failure finding is added only when there is no other finding; platform-file and cross-tenant findings have no consistent subject. Both attribution rules were broken by measured counter-examples: a neighbour's violation hid amtool's refusal of the written file; a broken `_routing_defaults.yaml` dropped the platform's enforced routes, and that finding belonged to no write at all |
| Compare only "which error messages are new" before and after the write | When a tenant that already violates the policy changes its config, it produces exactly the same message line as the base, and a diff would let it through |
| When a tenant already violates the policy, let the write through if the rendering is unchanged | It leaks: measured, adding an empty value to `group_by` makes the generator report one more blocking finding while the rendering is unchanged. Closing that would mean comparing whether findings are new — the row above. Kubernetes CRD validation ratcheting took five releases from 1.28 alpha to 1.33 GA and accumulated an ever-growing list of exceptions, which also shows how hard such exceptions are to converge |
| Validate a batch twice: drop the violating items, then validate the remaining combination | Depends on per-finding attribution (two rows up); replaced by all-or-nothing batches |
| Have the generator refuse aliases, merge keys and explicit tags | Its purpose was to narrow the gap between two parsers; with one parser there is no gap left to narrow, and it would make customers rewrite existing config files |
| `parse-tree` refuses everything today's generator refuses | It would mean rewriting every PyYAML check in Go, once again having Go imitate the other parser; comparing the two sides kept finding new "only PyYAML refuses" classes. Instead the list is closed, everything else is read per yaml.v3, and the migration audit lists it (§2) |
| A two-phase switch (old and new side by side, compare differences, delete the Go side once conditions hold) | The product has few adopters and can take breaking changes; the side-by-side period means maintaining two judges and a divergence catalog, and Go's judging is known to be looser, so keeping it buys nothing |
| The generator container parses the whole tree from scratch on every request, with no cross-request cache | Measured, 1000 tenants on a single core with amtool already take 9.4–9.8 s and 5000 tenants without amtool take 42 s, beyond the 20 s judging deadline, and about three quarters of the time goes to reading files ("Cost") |
| Judge inside the long-running process itself, without forking children | One oversized or malicious write would bring down the long-running process and make every later write get 503; children can each have their own memory limit and timeout and refuse only that one write |
| Ship all the steps in one release | Only possible as one huge PR covering every step, hard to review; or via an integration branch (next row) |
| Finish on an integration branch, then merge into main at once | CI runs only on PRs to main (the trigger in `ci.yml`), so no step on the integration branch would be checked by CI; doing this would first need the CI triggers changed |
| A switch toggling between the old and new judging | Both sides of the switch must be maintained and tested, recreating the rejected side-by-side period ("A two-phase switch" above) |
| Produce a resolved config JSON for tenant-api to read | No code needs to read it; whether policy is usable is reported by the status endpoint instead |
| Let Python parse all YAML and have the Go side read Python's JSON | The exporter is a runtime component that must run standalone in Go; depending on Python would put a Python runtime in every exporter pod |
| Commit the resolved policy into git as a separate file | Covers policy only; routing is still parsed by both sides. If someone forgets to regenerate it, a freshly started tenant-api blocks every write because it cannot read it, and a running one keeps the old policy |
| A custom config syntax shared by all three | Old YAML rules read real tenant ids such as `no` or `0777` as booleans or numbers; and every customer would have to rewrite every config file |
| Make the generator use a C-implemented parser | Only lines up the layer that splits text into tokens; gaps also sit in the layer that builds data from tokens |
| Port all of Python's routing and policy semantics to Go | About four to five thousand lines, a second semantic implementation; only considered if the cost of the extra container is unacceptable |
| Make the write API accept only JSON | Solves only tenant-api writes; customers' hand-written YAML still has to be parsed in CI and by the exporter |
| Have da-guard call the generator itself | Needs Python at runtime; the standalone binary without Python could no longer be used |

## Implementation order

- Done: the generator outputs structured findings.
- Done: CI checks (users of the deletion scope, the divergence catalog and its two-way tests). The divergence catalog was deleted in step 3.
- Step 0 (ship first, no behaviour change): `parse-tree` and the fixed corpus `tests/shared/parse_tree_corpus.json`; the migration audit command; moving the utilities unrelated to parsing.
- Step 1: the prerequisites listed in §7; the generator container (validation endpoint, status endpoint); the Helm chart adds the container, socket, policy-file mount and resources; tenant-api's writes ask the generator container, deleting tenant-api's Go-side routing and policy judging, the flat key `_routing_receiver_type` and `--policy-unavailable-open` at the same time.
- Step 2: da-guard adds the "not judged" state and exit code 4; `guard-defaults-impact.yml` also runs the generator.
- Done, step 3: deleted the Go code that imitates PyYAML and the generator's semantics, the vendored yaml.v3 "spaces only" patch, the divergence catalog and the expiry check in `make pre-tag`.
- Step 4: the generator and the Python tools read the parse-result JSON; delete the Python imitation loaders and the "non-specific tag" patch; the tools that write conf.d add the pre-write check; re-run the fixed corpus on main.

Steps 0 to 4 merge into main in order, and each can be released once merged (§7).

## Appendix: code locations

Where the code is, for implementers; can be skipped when reading the decision. Older code comments that say "ADR-036 step 2" (or 「第 2 步」) mean the CI checks already done in the implementation order above; 「實作順序第 4 步」 means tenant-api asking the generator container, which is now step 1.

| Term in this document | Code |
|---|---|
| The existing "Python calls a Go binary and reads its JSON" mechanism | `scripts/tools/_lib_tenant_values.py` (`served-values` / `effective`) |
| The Python YAML-reading and file-selection libraries (switch to `parse-tree`) | `scripts/tools/_lib_io.py`, `_lib_confd.py`, `_lib_yaml_keys.py` |
| Python loaders that imitate yaml.v3 | `_lib_yaml_keys.ExporterKeyLoader`, the `StrictExporterKeyLoader` family in `_lib_io`, `_GoKeyLoader` in `describe_tenant` |
| Tools that write conf.d (text produced by PyYAML; kept) | `StrictRewriteLoader`, callers of `strict_load_for_rewrite` / `load_yaml_file_for_rewrite` in `_lib_io` and `_lib_yaml_keys.dump_for_rewrite` (for example `patch_config`, `deprecate_rule`, `da_assembler`); `scaffold_tenant` and the others that create new files with `yaml.safe_dump` |
| Policy file names the generator reads on its own | `_POLICY_FILENAMES` in `_grar_parse.py`; the exporter skipping nested `_` files is explained in `flat_build.go` |
| The portal's preview-only parsing | js-yaml via `tools/portal/src/interactive/tools/_common/validation/yaml-parser.js`; the hand-written line-by-line parsing `tools/portal/src/interactive/tools/config-lint/lint.js` |
| Utilities unrelated to parsing, moved out in step 0 | `IsValidTenantID` (callers use `pkg/tenantid`), `LevelOf` and `IsDomainPolicyFile` (`internal/confdlayout`); the routing-profiles file names are `routingProfilesNames` in `cmd/da-guard` |
| Code that blocks duplicate keys and multiple documents | Python's `RejectDuplicateKeys`; on the Go side `parse-tree` (`internal/parsetree`), which da-guard reads for its undetermined evidence. The Go copies (`pyyamlcompat.FindDuplicateKey`, `firstDocument` / `errMoreDocuments` in `routingpolicy`, da-guard's `pyyaml_duplicates.go`) were deleted in step 3 |
| Go code that imitates PyYAML's reading (deleted in step 3) | `pkg/pyyamlcompat`; `pyResolve` and the policy decoder that turned on `SpacesOnly` in `pkg/routingpolicy` |
| Go code that imitates the generator's semantics (deleted in step 3) | `shapeWork`, `ParseDomainPolicies`, `UnmarshalPolicy`, `IsDisabled` in `pkg/routingpolicy`; da-guard's routing checks `internal/guard/routing.go` and their finding kinds |
| Changes in the vendored yaml.v3 | `third_party/yaml.v3`: spaces only (deleted in step 3), non-specific tag (delete in step 4), duplicate-key checking of large mappings (used by the exporter; keep); the pinning test `tests/ops/test_vendored_yaml_v3.py` is updated in the same PR |
| Exporter readings affected by the non-specific tag | `isMergeKey` in `pkg/config/config_file.go`; `validateSloObjective` in `custom_alert.go` |
| The Go copy of the receiver contract | `pkg/receiverspec`: `Known` (used by the exporter's routing resolution; kept); `Check` (used by da-guard and `pkg/routingpolicy`; deleted in step 3; tenant-api stopped using it in step 1) |
| tenant-api's current write judging | the generator container's caller `internal/generator` (`Gate`); on the Go side only the shape and reserved-key checks remain (next two rows) |
| Go re-emitting batch patches | `mergePatchYAML`; the invariant after the merge `verifyPartialWrite` (`handler/tenant_list.go`); the offline differential test `handler/batch_patch_diff_test.go` |
| The rest of tenant-api's Go-side routing and policy judging (deleted in step 1, done) | the routing-patch check `validateRoutingPatch`; the receiver check `receiverViolations` in `gitops` (`placeholderViolations` in the same file is the masked-credential write-back guard and stays); `JudgeTenantBlock` / `CheckTenantRouting` in `internal/policy`; `tenantBlockOnDisk`, which treats an unreadable file as empty |
| tenant-api's non-routing checks kept in step 1 | `validateShape` in `gitops/writer.go` (checks with yaml.v3 that the exporter can read the content); the reserved-key value checks in `handler/body_validator.go` (for example `validateSilentMode`) |
| Descriptions to update for da-guard's exit 4 | the exit-code comment in `cmd/da-guard/main.go`; `scripts/tools/_lib_exitcodes.py`; `docs/cli-reference.md` |
| The deletion-scope user check (CI) | declaration table `tests/shared/phase2_deletion_scope.json`; the test comparing it, `components/threshold-exporter/app/phase2_deletion_scope_test.go` |
| Checks only Go's da-guard had (none left: moved to the generator in step 2, deleted in step 3) | the generator's side is pinned by `tests/shared/routing_policy_parity_matrix.json` |
| The divergence catalog (deleted in step 3; the files and target below no longer exist) | `tests/shared/reader_divergence_catalog.yaml`; the Go verdict snapshot `tests/shared/merge_key_go_verdicts.json`; `tests/shared/test_reader_divergence_catalog.py`; `reader-divergence-expiry` in `make pre-tag` |

## References

- [#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766): design discussion, measurements and external review of the semantic layer
- [#2486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2486): decisions, counter-examples and measurements for the switch-over design and the write criterion
- [#2858](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2858): tracking issue for keeping one parser at the syntax layer
- [ADR-023 tenant-api write plane: the single-writer invariant](./023-write-plane-single-writer-invariant.md) (Chinese only)
- [ADR-037 config-diff Compares Rendered Output](./037-config-diff-compares-rendered-output.en.md)
- Industry practice:
  - Parse once, leave validation to the authoritative side: [Kubernetes server-side dry-run](https://kubernetes.io/blog/2019/01/14/apiserver-dry-run-and-kubectl-diff/).
  - Put the parsing tool into the user's pod: [Argo CD Config Management Plugins](https://argo-cd.readthedocs.io/en/stable/operator-manual/config-management-plugins/).
  - Refuse rather than allow when the validator fails: [Kyverno policy settings](https://kyverno.io/docs/writing-policies/policy-settings/).
