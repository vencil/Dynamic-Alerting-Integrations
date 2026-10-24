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

✅ **Accepted** (drafted 2026-10-09; every revision below was approved or decided by the owner).

- **Revised 2026-10-11**: the YAML syntax layer also keeps a single parser, and every read used to judge config content reads the JSON parsed by Go; a write goes ahead only when the whole tree afterwards passes the generator's check; no resolved config JSON; `--policy-unavailable-open` is removed; git is required.
- **Revised again 2026-10-11**: sets the `parse-tree` output envelope, its closed "unreadable" list and fixed corpus (§2), the error code when the generator container is unavailable (§5), and da-guard's "not judged" exit code (§9).
- **Revised 2026-10-12**: the generator container caches parse results across requests and runs each judgement in a separate process with memory and time limits, and sets the timeouts and the line between "refuse this write" and "container unavailable" (§3); batches become all-or-nothing and are judged and committed in a temporary git worktree (§4).
- **Revised 2026-10-14**: the parse cache key uses the SHA-256 of the file's content instead of git's blob SHA (§3).
- **Revised 2026-10-15**: when files are read from git objects, every entry that is not a regular file (a symlink, a gitlink, …) is judged `not_regular_file` (§3).
- **Revised 2026-10-15**: merges of batch patches and custom-alerts are guarded by an invariant on the result: outside the edit, every node of the result must be written exactly as in the original (§4).
- **Revised 2026-10-16**: "not judged" now also covers a tree with `_` keys that only the generator judges (§9), to go with [ADR-038](./038-unread-keys-judged-by-readers.en.md).
- **Revised 2026-10-20**: the generator now reads `parse-tree`'s result, so §2 and §9 drop the sentences describing its earlier PyYAML read; the decisions are unchanged.
- **Further revision 2026-10-20**: the Go side also keeps one judgement and one writer, YAML outside conf.d follows its consumer, and the migration audit command is deleted; details in §2, §3, §8, §9, §11, §12 and the order in §7.
- **Revised 2026-10-21**: tenant-api's writes and the admin config it reads itself go through the shared judgement (§7, step 7); §2 is rewritten to describe what is now in place, and says that the RBAC config is judged wherever it sits; the decisions are unchanged.
- **Further revision 2026-10-21**: the vendored yaml.v3 "non-specific tag" patch is deleted (§7, step 8; §11); the §7 paragraph on why it waits for step 8 is rewritten to describe what is in place; the decisions are unchanged.
- **Third revision 2026-10-21**: every `parse-tree` mode writes the same per-path index, with Go judging whether each path is a config file; the format becomes `da-guard.parse-tree/v2` (§2). §7 step 9 is rewritten; §3 "Whether the cache stays" states that what is measured is one complete judgement, served-values included.
- **Revised 2026-10-22**: the vendored yaml.v3 base moves to upstream `go.yaml.in/yaml/v3` (version in §11), which now provides the unhashable merge key fix (the §4 example, §7, §11, "Known limits and risks"); the decisions are unchanged.
- **Revised 2026-10-23**: §3 "Whether the cache stays" records the outcome: the cache stays, and its cold start reads the missing files in batches with `parse-tree`'s batch single-file mode; the consequences entry "Whether the cache stays is open" becomes "The cache stays".

## Summary

**Problem**: the same conf.d (the tenant config directory) is read by two YAML parsers: the Go side (exporter, tenant-api, da-guard) uses yaml.v3, the Python side (the route generator and the tools) uses PyYAML. They often read the same file differently; both sides wrote a lot of code to imitate the other, and several rounds of fixes have not closed the gap. With the generator reading Go's result, the Go side itself still has more than one "unreadable" judgement, the Python tools that write conf.d still read and write it with PyYAML, and YAML outside conf.d is still read by PyYAML imitating other readers (see "Problem").

**Decisions**:

1. **Only Go parses the YAML.** da-guard gains a command that reads a whole conf.d into one JSON document: which files were selected, the content of each file, and which files could not be read. When judging config content, the Python side (including the route generator) no longer parses conf.d YAML itself; it reads this JSON.
2. **The Go side also has one judgement.** The exporter, tenant-api and da-guard all pass conf.d, when loading it, through the same package that produces this JSON to decide which files can be read; the exporter does not serve a file it judges unreadable (§2). Which file names are tenant files, and what the tenant id is, also has a single implementation, in Go.
3. **conf.d also has one writer.** Rewriting existing files and creating new ones is done by da-guard; the Python tools only send "what to change" (§2).
4. **Each semantic area has one implementation.** Routing, domain-policy and tenant-list semantics are judged only by the generator (Python); thresholds and the values the exporter serves only by the exporter (Go).
5. The tenant-api pod gains a container that runs the generator, and every write that changes file content asks it first. tenant-api no longer judges routing and policy itself.
6. **YAML outside conf.d follows its consumer.** When Go or another program reads a file and acts on it, the Python side gets the content the way that consumer reads it, not with PyYAML of its own (§12).
7. **Merged in order on main, main releasable after every step.** The order is in §7: tenant-api first asks the generator container, then da-guard stops judging routing and policy, then the Go imitation code is deleted, then the generator and the Python tools read the JSON, so whichever commit is released there is no release where the generator already reads with yaml.v3 while Go still imitates PyYAML; after that the Go readers go through the shared judgement, writing moves to Go, and YAML outside conf.d follows its consumer. There is no side-by-side period and no switch.

**Impact on users and operators** (all breaking changes):

- Values written under the old YAML rules may change meaning, and some files become unreadable. For example `send_resolved: yes` in a route used to be read by the generator as the boolean `true`; parsed by yaml.v3 it is the string `"yes"`.
- The exporter no longer serves a file `parse-tree` judges unreadable (§2). Spellings the exporter reads today and will then not serve at all include more than one document in a file, a tag outside the allow-list such as `!!binary`, and a BOM in the middle of the file. Before upgrading, validate your conf.d in CI with the new da-guard: the files it reports with exit 3 are exactly the files the new exporter will not serve (§8).
- The tools that write conf.d produce yaml.v3's output: blank lines may be collapsed, and the alignment of end-of-line comments and the quoting style become yaml.v3's; more comments survive than today (§2).
- Python tools that read conf.d need da-guard. The da-tools image already bundles it; local pre-commit builds it with Go on the spot.
- tenant-api gains a container (the da-tools image). When the container or the deployment policy file is unavailable, every write that changes file content gets 503 (thresholds-only writes included); there is no switch to let writes through while unavailable.
- The whole tree after a write must pass the generator's check before the write goes ahead. While anything in the tree would be blocked, every write through tenant-api is refused until it is fixed (§4).
- Batch writes become all-or-nothing, released on their own before tenant-api starts asking the generator container (§4).
- tenant-api requires git; deployments without git are no longer supported.
- Files the generator used to refuse whole only because of PyYAML's limits are now read per yaml.v3 (§2).
- A CI that runs only da-guard no longer covers routing and policy, and needs an extra `da-tools generate-routes --validate --strict` step.

**Non-negotiable acceptance conditions**:

- No reader may be looser than the generator: it must neither read a policy the generator drops nor miss one the generator enforces. tenant-api's write criterion is the generator's own result (§4), so for tenant-api this holds by definition.
- `parse-tree`'s "unreadable" is a closed list (§2): it names only the forms different readers could read with different meanings; everything else is read per yaml.v3, including files the generator used to refuse only because of PyYAML's limits.
- For any one file, every Go reader reaches the same "unreadable" verdict: they all use the same package and the same rules (§2).

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

### The gaps left after the generator reads Go's result

With the generator and the Python tools that read conf.d reading `parse-tree`'s JSON, the same problem still shows up in these shapes:

- **The Go side itself has more than one judgement.** Only the generator uses `parse-tree`'s "unreadable" list. Each time the exporter loads, it decodes the same file into typed structures with yaml.v3 several times, without that list; tenant-api has its own shape rules (for example it accepts a trailing empty document that is only `---`). Measured on main on 2026-10-20 on 9 spellings, `parse-tree` treats all of them as unreadable:
  - The 7 the exporter reads: more than one document in a file (the tenants in the second document are silently dropped), `!!binary`, `!!set`, a BOM in the middle of the file, a null key (`~:`), the non-specific tag `!`, a trailing document that is only `---`.
  - The other 2 both sides refuse or report as an error: a duplicate key (both refuse), a non-scalar key (the exporter reports a decode error rather than panicking, thanks to the vendored yaml.v3's unhashable merge key fix, see §11).

  So the exporter may read a file the generator refuses.
- **The file-name rules have 4 implementations in Go** (which file names are tenant files, how a file name maps to a tenant id), and the Python side has a name predicate of its own, kept in line by a comparison table.
- **The Python tools that write conf.d are a third reader.** They read the original file with PyYAML, change it and write it back; making that read like yaml.v3 took about 27 rounds of patches, and the rewrite drops comments. A sample (2026-10-20) of reading and re-writing the 66 conf.d files in the repo: through yaml.v3's node tree, 53 come out identical to the original and the rest differ only in blank lines and the alignment of end-of-line comments; through PyYAML, 25 come out identical and 19 lose every comment.
- **For YAML outside conf.d, the authoritative reader is not yaml.v3.** The reader of a Kubernetes manifest is kubectl (decoding with sigs.k8s.io/yaml, which follows YAML 1.1's rules), which reads `name: no` as the boolean `false` while `parse-tree` reads the string `"no"`; Alertmanager reads its config with yaml.v2. Reading these files with PyYAML on the Python side is imitating another reader again. The repo already has a precedent: `da-crdecode` asks sigs.k8s.io/yaml directly instead of imitating it in Python.
- **The migration audit's "today" is a reading that was never released.** It takes PyYAML plus the loader layer imitating yaml.v3 as "today", but the released da-tools (`tools/v2.9.0`) has no such layer. What a customer really needs to know before upgrading is "what does the new version refuse, and how does its output differ".

## Decision

### 1. Who parses what

| Layer | The single implementation | How other tools get it |
|---|---|---|
| YAML syntax: which files are selected, the content of each file, which files cannot be read | Go's `parse-tree` package (yaml.v3) | Python reads the "parse-result JSON" da-guard outputs; the exporter, tenant-api and da-guard call the same package in process |
| conf.d file-name rules (which file names are tenant files, which tenant id they map to) | Go (one implementation) | Python reads `parse-tree`'s file list |
| Writing conf.d: rewriting existing files, creating new ones | Go (da-guard's `write`) | the Python tools send "what to change" |
| Semantics of routing (`_routing_defaults`, `routing_profiles`, a tenant's `_routing`), domain policy and the tenant list | the route generator (Python) | read the generator's findings (each problem the generator reports, see §4) and its status endpoint (§5) |
| Thresholds and the values the exporter actually serves | the exporter (Go) | unchanged: read the JSON of `da-guard served-values` / `effective` |
| YAML outside conf.d that another program reads | that consumer | read the way the consumer reads it (§12) |

The "thresholds" row is the existing precedent: thresholds already follow "one implementation, other tools read its output". This ADR applies the same approach to the YAML syntax layer, the file-name rules, writing, and routing and policy semantics.

### 2. The parse-result JSON

da-guard gains a `parse-tree` subcommand. It takes a conf.d directory and outputs one JSON document. It also has a single-file mode, used to read a write back before it goes ahead (see "Writing conf.d files" in this section); a list-only mode (`--list-only`), which reads no file's content; and a names mode (`--list-only --names`), which classifies file names that are not on a disk (for example the keys of a ConfigMap) by name alone. All four modes write the same kind of document.

**One entry per path**: the document's `entries` hold one entry for every path the walk records (the walk keeps one record per path: a directory with a config-file name that cannot be listed is `unlistable_dir`, never also `dir`; every directory the walk goes into has an entry, so no reader lists directories itself). The whole-tree mode and the list-only mode build them with the same code, so for the same tree the two agree field for field; they differ only in the problems that reading the content finds. Every entry carries every field, `null` where it has no value:

- `path`, `depth` (directories between the root and it), `class` (what the name makes it: `tenant`, `defaults`, `platform`, or `directory`; file and names mode also have `hidden` and `not_config_name`, which the walk never lists), `tenant_named` (the tenant id the file name carries; the tenants the exporter serves still come from the `tenants:` keys in the file), `defaults_document` (a platform-defaults document, its name starting with `_defaults`, validated by the platform-defaults schema), `config_extension` (the name carries a config file's extension, `.yaml` / `.yml` in any case; answered by the name for a hidden name and for a directory with a config-file name too).
- `readers`: who reads it (`exporter`, `generator`); an empty list when no reader does, with the reason in `not_read`. Names mode has no `readers`, and its only `not_read` is `unselected_defaults_carrier`: the given names are taken as one directory and selected as the walk selects, the carriers not chosen get that reason, and the chosen one is `defaults_carriers["."]`.
- `role`: what the path is, judged by Go; readers read it and never compose it themselves:
  - `config`: a config file, readable or not. When it cannot be read, `problem` says why, and a reader reading it fails loudly.
  - `dir`: a directory the walk goes into and can list. A directory with a config-file name (such as `x.yaml/`) is a `dir` too — the exporter goes into it and reads the files under it — and carries a `problem` (`not_regular_file`) saying that its name looks like a file's.
  - `not_a_file`: the exporter's walk cannot reach a regular file there and does not go into it: a symlink to a directory, a special file such as a FIFO, or a stat that finds nothing (a dangling link, a looping link, a chain of more than 40 links).
  - `unlistable_dir`: a directory whose contents cannot be listed (`.` for the root). Nothing under it was seen, so the document is incomplete.
  - The judgement is a whitelist: only the cases above are `not_a_file`; every other failure (permission denied, EIO, a name too long, a reason added later) is `config`. The default the other way would let a tenant file whose stat a permission refuses vanish from the list behind one WARN line.
- `problem`: why a read of it fails, `{kind, message}` (see "Unreadable files" below).
- `verdict`: how da-guard's validation (without `--scope`) lists the path, computed in Go from validation's own lists: `unservable` (listed under exit 3: the exporter's load drops it or cannot read it, so it cannot serve it), `tree_refused` (listed under exit 4 as `unreadable_outside_run`: the route generator refuses the whole tree on it), or `null`. A reader that must fail on every path validation lists reads this field and composes nothing from `role` and `problem` — a file the exporter's decode refuses can have no `problem` at all (e.g. a list under `tenants:`). The list-only mode reads no content and knows only the walk's half: a file refused for its content is `null` there; file mode gives the full verdict for `--path` alone and loads no tree. A broken `_defaults` carrier below the root that its directory does not select is both `unselected_defaults_carrier` and `unservable`. `verdict` answers per path only: `null` on every entry does not mean validation passes (a tenant declared twice, routing content, fall on no path); for the whole verdict, run validation.

Example: a tree holds `t1.yaml`, `_defaults.yaml` and `_defaults.yml`, `team/t2.yaml` with a syntax error, and `gone.yaml`, a link to a file that does not exist. `da-guard parse-tree --list-only --config-dir conf.d` writes (three entries shown, one per line):

```json
{"path": "_defaults.yml", "depth": 0, "class": "defaults", "tenant_named": null, "defaults_document": true, "config_extension": true, "readers": [], "not_read": "unselected_defaults_carrier", "role": "config", "problem": null, "verdict": null}
{"path": "gone.yaml", "depth": 0, "class": "tenant", "tenant_named": "gone", "defaults_document": false, "config_extension": true, "readers": ["exporter", "generator"], "not_read": null, "role": "not_a_file", "problem": {"kind": "read_failure", "message": "the path cannot be statted (stat_error)"}, "verdict": "unservable"}
{"path": "team/t2.yaml", "depth": 1, "class": "tenant", "tenant_named": "t2", "defaults_document": false, "config_extension": true, "readers": ["exporter", "generator"], "not_read": null, "role": "config", "problem": null, "verdict": null}
```

The whole-tree mode gives the same three entries for the same tree, except that `team/t2.yaml` also has `"problem": {"kind": "syntax_error", "message": "yaml: line 2: did not find expected node content"}` and `"verdict": "unservable"`; it adds `files` and `rendered_by`. `gone.yaml` has the same `problem` as a file whose stat a permission refuses; only `role` tells that it is not a file.

**Which files were selected**: `readers` marks every file some reader actually reads.

- The exporter's file-selection rules (extension, case, hidden files, subdirectories), including which `_defaults` file each directory finally uses.
- Files the generator reads and the exporter does not: policy and routing-profile files (`_domain_policy.yaml`, `_routing_profiles.yaml`, `_profiles.yaml`, including those in subdirectories and the `.yml` spelling). When both spellings of one file exist, the generator's current handling applies.

The Python side no longer has its own copy of these rules. Differences between the two sides' selection go into the fixed corpus.

**The file-name rules live in one place, in Go**: which file names count as tenant files, and how a file name maps to a tenant id, has a single implementation on the Go side, shared by the exporter, tenant-api and da-guard. Tools that need only the list of files, not their content, use a `parse-tree` mode that lists files without parsing them; a tool that already holds the whole-tree document reads its `entries` instead of listing the files a second time. The Python name predicate and the tests comparing it with the Go rules retire with it; the name table itself stays, as a fixture of the Go tests.

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

**Unreadable files**: the path's entry has a `problem` of `{kind, message}`, and `files` holds no content for it. `kind` is a fixed code, one for each case below (13 in all, given in parentheses), which readers classify by; `message` is the explanation for people. Each of these counts as unreadable, rather than taking what yaml.v3 read:

- A read failure (`read_failure`), not a regular file (`not_regular_file`), not valid UTF-8 (`invalid_utf8`), a syntax error (`syntax_error`).
- A top level that is not a mapping (`top_level_not_mapping`). An empty file counts as an empty mapping and is not included.
- Anything yaml.v3 decoding reports as an error (`decode_error`), including alias expansion beyond the limit, an explicit type that does not fit the text (`!!int abc`) and a merge key whose value is not a mapping (`<<: 1`). PyYAML reads some of these (for example `!!bool yes` as `True`); afterwards they are all unreadable.
- More than one document in a file (`multiple_documents`, separated by `---`). yaml.v3 reads only the first and silently ignores the rest; copying that would disable any policy after the `---`.
- A duplicate key within one mapping (`duplicate_key`), compared by key source text after alias expansion. yaml.v3 silently keeps the last value when the duplicate comes from an alias used as a key, so `parse-tree` checks this itself.
- A key resolved as null (`null_key`: `~:`, `null:`, an empty key), or a key that is not a scalar (`non_scalar_key`: `? [a]`, `? {c: d}`). Source-text output could not tell `~` from `"~"`; yaml.v3 also silently keeps the last of `~:` and `null:` in one mapping; and the generator and the exporter have handled null keys differently.
- A tag outside the allow-list (`tag_not_allowed`). Scalars accept only `!!str`, `!!int`, `!!float`, `!!bool`, `!!null`, `!!timestamp`; mappings and sequences accept only no tag or `!!map` / `!!seq`. `!foo`, `!!python/...`, `!!binary`, `!!set`, `!!omap`, `!!merge` are all unreadable: yaml.v3 reads several of them as plain strings, a different meaning from what the author or PyYAML meant.
- YAML's non-specific tag `!` (`non_specific_tag`, for example `! "x"`, `! "<<"`). It is outside the allow-list and unreadable at every step: reading `! "<<"` as a merge key or as a literal key could each silently add or drop policy. `parse-tree` detects `!` from the source text at each node's position, not from the node's tag, so it does not depend on the vendored yaml.v3 "non-specific tag" patch; after the patch is deleted, `! "x"`, `! "<<"`, `! |`, `! 1`, `! {…}` and `!<!>` are still unreadable. The fixed corpus covers these spellings.
- A BOM (`bom_not_at_start`; U+FEFF, an invisible character that marks the encoding) anywhere but the start of the file. When the BOM straddles byte 512, yaml.v3 swallows the next character (measured: `domain_policies` read as `omain_policies`, silently disabling the whole policy); anywhere else it can leave an invisible character in a key or value. Rather than tell the cases apart, any BOM past the start of the file makes it unreadable.

**The envelope**: `schema` is `"da-guard.parse-tree/v2"`, alongside `da_guard_version`, `mode` (`tree`, `file`, `list`, `names`), `defaults_carriers` (the one `_defaults` file each directory uses) and `entries`; the whole-tree and single-file modes also carry `files` and `rendered_by`. A reader that meets any other `schema` value, or an entry missing a field, treats the whole document as unusable and says that the da-guard is older than the tool. Adding a field does not change the `schema` value; only a change to the shape a reader relies on does, the same convention as `served-values` / `effective`.

**The Go readers follow this judgement too**: the code that decides which files can be read is one Go package, placed where the exporter, tenant-api and da-guard can all use it. All three pass conf.d through it when loading, then each decodes the readable files into the structures it needs. The YAML tenant-api decodes itself goes through it too: the body of a write, the file a whole-file write replaces, and the admin config it reads itself (`_tenant_orgs.yaml`, the RBAC config, groups, views, the federation whitelist and per-tenant subsets, the AccountID registry).

- The RBAC config is judged whether or not it sits in conf.d: tenant-api is its only reader (§12), and reading it two ways depending on where it sits would be two loaders for one file.
- Groups, views, the federation whitelist, the subsets and the AccountID registry live in tenant-api's state store, not in conf.d; their content is either an old conf.d file imported as is on upgrade or text tenant-api wrote itself, and it is judged the same way.
- For a file judged unreadable, each reader keeps its existing parse-failure handling and never reads it as empty (most of these are inputs to the authorization boundary, and an empty RBAC or organization map is a different grant, not a missing config): the RBAC config and `_tenant_orgs.yaml` fail startup and keep the previous version on a hot reload; groups, views and the whitelist log a WARN at startup and start from an empty config, and keep the previous version on a hot reload; a write to groups or views answers 500 `CONFIG_DECODE` and does not rebuild from the unreadable record; the AccountID registry is handled as a registry that cannot be read (the import fails startup, allocation and the startup checks return an error).
- So no Go reader uses as configuration what a decode gave before the judgement. The decodes that still run before it only refuse or classify (for example whether a tenant file declares only one tenant, or whether its text must be withheld from the caller); their outcome can only refuse a request or hide content, never let something through.

- For a file judged unreadable, the exporter does what it does today for a syntax error in the same kind of file, with the log and the metric also carrying the `kind`:
  - A tenant file: the tenants it declares are not served, and the other tenants of the tree are unaffected; one WARN log line, and `da_config_parse_failure_total` goes up by one.
  - A file of the `_defaults` chain or another platform file starting with `_`: skipped whole, one ERROR log line ("entire block dropped"), and `da_config_parse_failure_total` goes up by one; merging the defaults chain reads it as an empty file, and `da-guard effective` lists it in `chain_parse_failed`. The tenants depending on it still load, only without the defaults that file provides: a threshold served only because of that file's default is no longer served.
  - Measured (2026-10-21, da-guard at head): with a syntax error in the root `_defaults.yaml`, `served-values` lists it in `parse_failed`, tenant `t1` is still there but `mysql_connections`, whose default came from that file, is no longer served, and `effective`'s `chain_parse_failed` is `["_defaults.yaml"]`; with a syntax error in the tenant file `t2.yaml`, `t2` is absent from `tenants` while `t1` is unaffected. da-guard returns exit 3 in both cases.
- tenant-api's read side (`/effective`, the tenant list and the other endpoints that read conf.d through `pkg/config`) switches to this judgement together with the exporter. tenant-api's writes have no shape rules of their own: a body judged unreadable gets 400, like any other body validation failure, with an error naming the `kind` and the line but not quoting `parse-tree`'s message (that message quotes the refused key or value, and a batch patch validates the whole file, which several tenants may share). A file a whole-file write replaces that is judged unreadable counts as a file that does not parse: replacing it needs write permission on every tenant.
- da-guard reports these files with exit 3 (§9).
- So no file is "read by the exporter, refused by the generator": the generator, the exporter, da-guard and tenant-api refuse the same files.

This is a breaking change: of the 9 spellings measured in "Problem" above, the exporter reads 7 today, and afterwards those files are not served at all. Example. Input:

```yaml
# t1.yaml
tenants:
  t1:
    mysql_connections: "80"
---
tenants:
  t2:
    mysql_connections: "90"
```

- On main at the time of this revision (2026-10-21): `parse-tree` judges this file `multiple_documents` ("a second document starts at line 4; only one document per file is read"); the exporter reads the first document and serves `t1`, and `t2` is silently dropped (the `tenants` of `da-guard served-values` holds only `t1`); da-guard run alone returns exit 4 and lists the file as evidence only the generator judges.
- Afterwards: the exporter serves neither `t1` nor `t2`, and a metric and a log name the file and `multiple_documents`; da-guard returns exit 3. The fix is to move the second document into a file of its own.

**"Unreadable" is a closed list**: only the 13 cases listed above are unreadable. They are the forms the remaining readers could read with different meanings; those readers are `parse-tree`'s judgement itself, the step in which each Go reader, after that judgement, decodes into typed structures with yaml.v3, the Go edit commands used for writing (see "Writing conf.d files" in this section), and the portal's js-yaml previews (never a basis for letting anything through). Everything else is read per yaml.v3, including files the generator used to refuse only because of PyYAML's limits, for example:

- An anchor name defined twice (`a: &x 1` and `b: &x 2`)
- A date that does not exist (`2001-13-45`)
- A flow value ending in a colon (`{b: 1:}`)
- `\'` inside a double-quoted string
- A block scalar header followed directly by `#` (`|#c`)
- TAB separators (a TAB after `a:`)

These files used to be refused whole by the generator; they are now read per yaml.v3.

Why `parse-tree` does not refuse everything the generator used to refuse: that would mean rewriting every PyYAML check in Go, exactly what this ADR rejects for two parsers; feeding random input to both sides and comparing kept finding new "only PyYAML refuses" classes. The riskiest example is `<<:` inside a flow mapping (for example a tenant `_routing` written `{<<:, receiver: {...}}`): PyYAML refuses the whole file, and yaml.v3 reads a literal key `"<<:"` (with a null value). Under yaml.v3's reading nothing is merged in, and the generator reports "'<<:' is not read by the route generator" and blocks the file with exit code 1; the failure goes toward blocking, not letting through.

The fixed corpus is `tests/shared/parse_tree_corpus.json`, built like the existing `merge_key_policy_corpus.json`: a Python test generates it and records the PyYAML version it was generated with. Each row stores the file text, the generator's verdict under the PyYAML reading (a frozen record; today's generator can no longer compute it), and the category `parse-tree` should give (unreadable with which `kind`, or read); the test checks that category, and every `kind` in the closed list has at least one row. Once the Go readers go through the shared judgement, the same corpus also confirms that every Go reader refuses the same files as `parse-tree` ("How it is accepted").

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

The generator reads this JSON, so `010` is `"010"` and `send_resolved: yes` is the string `"yes"` (PyYAML would read the boolean `True`): every Python tool sees exactly what the Go side read.

**How the Python side gets it**: through the existing "Python calls a Go binary and reads its JSON" mechanism (da-tools already reads da-guard's `served-values` / `effective` this way). The shared YAML-reading and file-selection libraries read this JSON instead; the tools no longer call a YAML parser on conf.d directly. Every place the generator uses a value switches to the JSON's type and value, not just `send_resolved` (for example `_severity_dedup: off` used to read as `False` and is now the string `"off"`).

**Writing conf.d files: Go is the only writer.** The tools that rewrite existing files (for example `patch_config`, `deprecate_rule`) and those that create new ones (for example `scaffold_tenant`, `init_project`, `onboard_platform`, `migrate_rule`) decide only "what to change" and hand the writing to da-guard:

- `da-guard write` rewrites an existing file with three operations: set (set the key at a path to a value), unset (remove the key at a path) and ensure-map (make sure a path is a mapping, creating it if missing). Editing happens on yaml.v3's node tree: only the edited nodes are replaced, and every other node and comment is written back as it was.
- `da-guard write` also creates a new file from ordered nodes the caller gives, keeping the caller's key order. Comments go where yaml.v3's comment model puts them: a mapping key and a sequence item may each carry a comment above it and one at the end of its line, and the file may carry one at its head and one at its foot (after the last node); comments are layout, not content, and are not compared on the read-back.
- `da-guard write` also moves an existing file, byte for byte, to a new path; the source is gone after.
- `da-guard write` also replaces a file whole: the text written is exactly the nodes and comments the caller gives; no node or comment of the old file is kept, and the old file is not read as a base, so a file whose text `parse-tree` cannot read can still be replaced (replacing it repairs it; a file whose bytes this process cannot read is still refused, as they are compared before the rename); the old file's mode and owner are kept. This is for files regenerated whole.
- `da-guard write` also deletes an existing file; with no file at the path it does nothing. A delete is judged like every other operation: the tree without the file must not be worse (deleting the selected `_defaults`, so that another spelling in the same directory is selected and cannot be read, writes nothing).
- Rewrites, creates, replaces, moves and deletes can go in one call, judged as one batch. When the write stops part-way, what can be undone (creates, moves, deletes) goes first and is undone; what was renamed over the old file (rewrites, replaces) cannot be, and is reported as written.
- These edit primitives are one shared Go package, extracted from the node editing tenant-api uses to merge custom-alerts and batch patches; §4's merge invariant (outside the edit, nodes are written exactly as in the original) applies to them as well.
- Reading the write back happens inside Go: one call may carry several files, and the batch is judged once — the tree is walked once with every file's text in place at its path and judged by `parse-tree`, and each file's content must equal "the original file's parse result with this change applied" (for a new file, the content the caller gave); if any does not, no file is written. Files of one batch bear on each other (which `_defaults` a directory selects, for one), so the batch is judged as a whole, not file by file against the disk. A write may repair the tree, never worsen it: when the tree after the call has a problem the tree before it did not (another file losing its readers, a new state validation names on any path, a new error of da-guard's validation such as a tenant declared in two files), nothing is written; a problem already there elsewhere does not block an unrelated write. Nothing is written either when the original file is unreadable or when no reader would read the target path.
- What to change, and whether to change it, the Python tools decide from the parse-result JSON; they no longer read the original or produce text with PyYAML, and the PyYAML rewrite layer is deleted together with the loader imitating yaml.v3 it depends on (§11).

Consequence: the written format changes. When yaml.v3 re-emits a file, blank lines may be collapsed, the alignment of end-of-line comments is normalised, and the quoting style of written values is yaml.v3's; but more comments survive than with a PyYAML rewrite (sample in "Problem"). A file holding spellings such as unquoted `1e3` or `0x1F` no longer has to be changed by hand to a quoted spelling before it can be written: nodes outside the edit are written back as they were (§4's invariant).

**Scope**: this section covers conf.d only; other YAML is in §12. The portal's in-browser tools (config diff, routing trace, YAML validator, playground and alert preview parsed with js-yaml, plus the hand-written line-by-line parsing in the config checker) are previews only and out of scope: their results are never the basis for letting a write or a CI run through, and their screens say that da-tools' result is authoritative.

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

The structure inside the container, conclusion first: a long-running process (a service process that stays up and keeps state across requests) checks files out and caches parse results, so a write does not re-parse the whole tree (whether this cache stays is decided by measurement, see "Whether the cache stays" below); each judgement goes to a new child process (copied from the long-running process, exiting after one judgement) that finishes within a memory limit and a judging deadline, one at a time. A child going wrong refuses only that one write; only the long-running process going wrong makes the whole container unavailable. Details follow.

- **Image**: the existing da-tools image, which already contains the generator and da-guard. The service uses only the Python standard library.
- **Communication**: HTTP/1.1 over a Unix socket; no network port. The socket lives in a shared scratch directory (emptyDir) inside the pod. Only processes that mount that directory and pass the socket file permissions can connect. The chart's existing human socket (for requests carrying a person's identity) works the same way.
- **Execution**: the long-running process checks files out of git and caches each file's parse result across requests, so each write re-parses only the files it changes.
  - The cache was adopted while the generator read files with PyYAML, when re-parsing the whole tree each time exceeded the 20 s judging deadline ("Cost"); whether it stays now that the generator reads `parse-tree` is in "Whether the cache stays" below.
  - **Judging happens in a child process**: for each request the long-running process forks a child that does the judging. Forking is how the operating system copies a process: the child starts with the same memory as the long-running process, so it inherits the cache as it stands. A batch is validated within that one child.
  - **One judgement at a time**: the long-running process lets only one child judge at a time; other requests queue, and time spent queuing counts toward the judging deadline (see "Deadlines" below).
  - The changed files are parsed in the child too, under the same memory limit and judging deadline; successful parse results are handed back to the long-running process and added to the cache. The long-running process never parses content that a write request brought in.
- **Cache**: a content-addressed cache, that is, a cache keyed by a hash computed from the content itself.
  - The key is (the SHA-256 of the file's content, the parse mode). It is computed from the file's bytes, so the same content always gives the same value, committed or not. Not git's blob SHA: in a SHA-1 repository a blob SHA can be deliberately collided, two different contents would share one key, and the parse of a refused content would be used to judge the other — a way around the judgement. The parse mode is which parser and which output form (today, `parse-tree`'s output for a single file). The same content read by different parsers gives different results, so the parse mode is part of the key.
  - Why it never serves stale content: the key is computed from the content itself, so changed content gets a different SHA and can never pick up an old result; no expiry by time or by commit is needed. The parser version does not change within one process, so the result for a given key does not change either.
  - Only the decoded content structure is cached. Verdicts that depend on the file's path, such as which files are selected and which are unreadable, are not cached and are recomputed on every judgement.
  - The parse results a child hands back to the long-running process must be as they were before the generator modified anything (a deep copy or the serialized result). Whatever the child changes in the cache it inherited disappears when the child exits and does not affect the next judgement.
  - The deployment policy file is not in git; it is not in this cache and is re-read on every judgement.
  - **Eviction**: blobs that appear in the tree of the latest base commit are always kept. Those not in that tree (for example content brought in by refused writes) are capped at 32 MiB in total, measured as the byte size of the serialized results the children hand back; when the cap is reached the least recently used entry goes first (LRU). A cache that only grows would make the long-running process's memory grow without bound.
- **Whether the cache stays**: the cache holds Go's (`parse-tree`'s) output; it is not a second reader, so whether it stays is a performance decision with no bearing on "one parser". Two measurements (samples from 2026-10-20) pull against each other:
  - Cold start (the first fill at startup or after a new base commit): the single-file mode walks the whole tree each time to decide file selection, so running it once for each file that misses makes the time grow with the square of the number of files (O(N²)). The files that miss are therefore handed to `parse-tree`'s batch single-file mode (`--inputs`): each batch walks the tree once, each file's line is byte for byte what the single-file mode prints for it alone, and the cache's keys, its content and the verdicts are unchanged.
  - Without the cache, running the whole-tree mode on every judgement leaves less than 8% headroom at 5000 tenants within the child's 192 MiB budget, partly because `parse-tree` buffers its whole JSON output in memory.
  - How it is decided: first make `parse-tree` stream its output (write as it produces, without assembling the whole document in memory), then measure one complete judgement on a realistically shaped 5000-tenant tree with amtool, with the child's `RLIMIT_DATA` at 192 MiB and the container at 1 CPU.
  - "One complete judgement" is every program the generator container has to run for one write: `parse-tree` in whole-tree mode, the generator, amtool, and the served-values that item 3 of [ADR-038](./038-unread-keys-judged-by-readers.md)'s rollout order runs there once it wires the shared verdict into the generator container. The measurement before removing the cache must include served-values; measuring only part of it overstates the headroom.
  - Before that, served-values must first offer a way to compute only the affected tenants (today it computes every tenant of the whole tree each time), and the measurement uses that way.
  - When measuring, the memory limits and the judgement deadline are taken from the Helm chart and the code themselves, with no copy of the numbers; when a limit or the deadline changes, the measurement follows.
  - The cache goes when both hold: the child and each program it starts peak at no more than 144 MiB each (75% of 192 MiB); the judgement takes no more than 10 s (half the 20 s judging deadline, the other half left for queuing and tenant growth). Then the container and the CLI take the same whole-tree path, and this section's cache, its eviction rules and the cache-size metric are removed.
  - Otherwise the cache stays, and only the cause of the O(N²) cold start is fixed.
  - **Outcome**: the conditions do not hold (measured with `scripts/tools/dx/measure_judgement_budget.py`; the numbers are in [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)), so the cache stays; the cold start takes the batch single-file mode above, with a number of batch calls that does not grow with the file count, in time that grows linearly with the number of files. The cold-start measurements before and after are in [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206).
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
  - A cap on the total output of `parse-tree` over the whole tree is set together with the measurement in "Whether the cache stays" above.
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
  - Before this change, batches are judged item by item: the violating items are marked as errors and the rest are written. Switching to all-or-nothing is a breaking change released on its own before tenant-api starts asking the generator container: the prerequisite that lets git writes commit several files at once (§7) already switches to all-or-nothing, still judged by Go's existing checks at that point; asking the generator container only switches the criterion to the generator, and the all-or-nothing semantics do not change.
- **Merging batch patches**: Go still reads the file, applies the patch and writes it back; the merged content is always validated. The node editing that does this becomes a shared package, which da-guard's `write` uses too (§2).
  - After the merge an invariant must hold, or nothing is written: the original file and the merge's result are both parsed into yaml.v3 node trees, and outside what this edit replaces every node must be written the same: kind, style (flow or block, plain or which quotes), the tag as written (an explicit tag's text, or none written), a scalar's text, the anchor name, and the node an alias points to (compared by content, not only by name). The documents must be as many, and the file must define the same anchors in the same order. The same characters mean the same to every reader, so the rule does not depend on any reader. The edited keys are also decoded and checked: a key set holds its new value and a key removed is gone, because a `<<` merge key can still bring in a removed key. Comments and positions are not compared.
    - Any difference refuses the write with `TENANT_CONFIG_NOT_LOADABLE` (409 for `PUT .../custom-alerts`; for a batch, that operation's code and 422 for the batch) and `config_error` `not_round_trippable`; the message names the first difference, as a path only inside the tenant's own block and as "another place" anywhere else, which may be a tenant the caller cannot read.
    - Example: `k: [1:20]` in another tenant's block. yaml.v3 re-emits it as `['1:20']`; PyYAML reads the original as the integer 80 and the new file as a string. The node's style changed from plain to single-quoted, so the write is refused. The same invariant refuses a folded block scalar whose more-indented line is rewritten, a tenant block shared through an alias that would change along with it, a repeated anchor re-pointing an alias, and a value a `<<` still supplies after the explicit key is removed.
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
- A policy file can declare "requires at least generator version X"; a container older than that refuses every write that changes file content; the fix is for the operator to raise the da-tools version set in the chart. This is a new key, read only from the deployment policy file (`--policy`), never from a policy file in the tenant repo; it is read by the route generator and by the CLIs that judge receivers by the same `--policy` (`validate-config`, `test-notification`, `explain-route`, which share the generator's reading function), each refusing to judge when it is older; it must be added no later than the step where tenant-api starts asking the generator container (§7), or older generators will not recognise it.
- tenant-api exposes the generator and da-guard versions as metrics.

### 7. Merge in order on main

**The steps merge into main in order.** There is no side-by-side period, no switch, and no divergence catalog (a list recording each config file Go and the generator judge differently). Why not do it all in one release: see "Considered and rejected".

Steps 0 to 8 are done, and each could be released once merged; the "non-specific tag" patch was not deleted in step 4 and is scheduled for step 8 (why: see below).

- **Step 0: ship first, no behaviour change.** `parse-tree` (single-file mode included) and its fixed corpus (§2); the migration audit command (§8); move the small utilities unrelated to parsing (for example the tenant id check, finding which directory level a file sits at, recognising policy file names) out of the packages to be deleted.
- **Step 1: tenant-api's writes ask the generator container.** The same step deletes all of tenant-api's Go-side routing and policy judging (§11), including the imitation that decides whether `_routing` is a string that turns routing off. Checks that are not routing judging stay: the guard against writing back masked-credential placeholders, the yaml.v3 check that the exporter can read the written content, and the value checks on reserved keys such as thresholds. The last two keep tenant-api from letting through files PyYAML reads but yaml.v3 does not (for example `"x\/y"`); duplicate keys the Go imitation package used to block are now blocked by the generator's own duplicate-key refusal. The same step also removes `--policy-unavailable-open` and deletes the flat key `_routing_receiver_type` (the old single-level way to set the receiver type); writing it now returns 400.
- **Step 2: da-guard reports "not judged"** (§9); `guard-defaults-impact.yml` also runs the generator.
- **Step 3: delete the Go code that imitates PyYAML and imitates the generator's semantics** (§11), along with the divergence catalog and the expiry check in `make pre-tag`. The vendored yaml.v3 "non-specific tag" patch (the change that keeps YAML's `!` marker on the node) stays in this step.
- **Step 4: the generator and the Python tools read the parse-result JSON.** Delete the Python loaders, imitating yaml.v3, used to read conf.d; the tools that write conf.d add the pre-write check (moved into Go in step 10, §2). The shared Python libraries and the tools (the generator included, which uses the shared libraries directly) switch together in this step; switching the shared libraries first would leave the generator reading half JSON, half PyYAML.

**Guarantee**: from step 3 on, no Go code produces routing or policy verdicts (findings); what the exporter keeps is only the lookup of whether a receiver type exists. Between steps 1 and 2 there are two judges side by side, Go (da-guard) and the generator, no looser than today. The generator reads the yaml.v3 way only from step 4, when the Go code imitating PyYAML is already deleted; so whichever commit is released, there is no release where the generator already reads with yaml.v3 while Go still imitates PyYAML.

**Why `parse-tree` is step 0**: part of what blocked duplicate keys, more than one document and tags outside the allow-list was the Go imitation code. Before step 3 deletes it, `parse-tree` must already treat these as unreadable (§11).

**From step 5: one judgement and one writer on the Go side, and YAML outside conf.d.** Merged into main in order, under the same discipline as steps 0 to 4: after each step merges, main must be releasable.

- **Step 5: `parse-tree`'s judgement moves to a shared place.** From inside da-guard into a Go package (under `pkg/`) the exporter, tenant-api and da-guard can all use; behaviour unchanged.
- **Step 6: the exporter and da-guard pass conf.d through this judgement when loading** (§2), and tenant-api's read side through `pkg/config` (`/effective`, the tenant list and so on) switches in the same step. The exporter does not serve unreadable files and reports them with a metric and a log; da-guard reports them with exit 3 instead of listing them under exit 4 as evidence only the generator judges (§9). This is a breaking change; how to guard an upgrade is in §8.
- **Step 7: tenant-api's writes use this judgement**, replacing its own shape rules; in the same step the admin config tenant-api reads itself (`_tenant_orgs.yaml`, the RBAC config, groups, views, the federation whitelist and subsets, the AccountID registry) passes through it before it is decoded. Most of these are inputs to the authorization boundary: a file judged unreadable gets each reader's existing parse-failure handling, never read as empty (§2).
- **Step 8: delete the "non-specific tag" patch.** By now the exporter, da-guard and tenant-api all go through the shared judgement (why: see below).
- **Step 9: the file-name rules and the file list converge.** The 4 Go implementations of the file-name rules become one. Every `parse-tree` mode writes the same per-path index (`entries`), with Go judging whether each path is a config file (`role`); the list-only mode is the same document without the content. The Python side has one place that reads the document; it reads `role` and no longer judges on its own whether a path is a config file; the name predicate and the tests comparing it with the Go rules retire, while the name table stays as a fixture of the Go tests. Tools that need a file list use the list-only mode, a tool that already holds the whole-tree document reads its `entries`, and the places on the Python side that still list conf.d's files by name rules of their own go through da-guard as well.
- **Step 10: the edit primitives and da-guard's `write`** (§2). The Python tools that write conf.d switch, in batches, to sending "what to change", and reading the write back moves into Go.
- **Step 11: delete the Python layer that rewrites conf.d with PyYAML**, together with the loader imitating yaml.v3 it depends on (§11).
- **Step 12: YAML outside conf.d follows its consumer, and a lint guards the boundary** (§12); the mechanism for getting tenant-api's admin config is decided here. §12's approach (an AST lint lists the call sites; an allow-list gives each a reason) is already applied to the conf.d readers, but only where it can decide: after step 9 deleted `_lib_confd`'s name predicates, the same kind of gate keeps them gone: `_lib_confd` defines none of them and nothing under `scripts/` uses one (`tests/shared/test_confd_tools_no_python_judgement.py`); whether a tool judges conf.d paths itself some other way is a behaviour question, answered by the behaviour matrix, not by matching text.
- **Step 13: delete the migration audit command** (§8); the same step adds the pre-upgrade checks to the incremental migration playbook.
- In parallel with the above: the measurement in "Whether the cache stays" (§3). The vendored yaml.v3 base has moved to upstream `go.yaml.in/yaml/v3` v3.0.5 (§11).

Why this order: steps 6 and 7 use the shared package, so step 5 comes first; step 6 is the breaking change users see, so it goes first to be released and guarded on its own; the "non-specific tag" patch is deleted only once every Go reader goes through the shared judgement (step 8); before step 11 deletes the rewrite layer, every tool that writes conf.d must already send intent (step 10).

**Why the "non-specific tag" patch waits until step 8**: the patch makes yaml.v3 keep the `!` marker on the node of a quoted string, and the exporter's merge-key check relies on it to take `! "<<"` as a merge key, as PyYAML does. Without it yaml.v3 drops the marker, and `! "<<"` is the literal key `<<`, merging nothing. Deleting it before the exporter goes through `parse-tree`'s judgement would silently change files the exporter reads. From step 6 the exporter, and from step 7 tenant-api, decode only files `parse-tree` judges readable, so a file with `!` never reaches their decoding; deleting the patch only once both have switched (step 8) means no commit on main has a component read `!` with a yaml.v3 lacking it.

**What must be done before step 1**:

- The config `da-tools init` generates pins the da-tools version.
- tenant-api's git writes support committing several files at once, and batches become all-or-nothing at the same time, released on their own (§4).
- The generator container (validation endpoint, status endpoint); the Helm chart adds the container, socket, policy-file mount and resources.
- The checks only Go's da-guard has move into the generator (§9).

**No falling back to Go when the container is unavailable**: tenant-api's Go judging is deleted in step 1; and anyone who can send writes can affect whether the container is available (for example by sending many requests that need whole-tree recomputation), so falling back to a looser judge would let the requester pick the judge.

The customers' conf.d trees are not available, so validation before each step merges covers only the owner's tree. How customers see the impact on their own tree before upgrading is in §8.

### 8. Migration: knowing the impact before upgrading

Once yaml.v3 parses the YAML, any value the two parsers read differently changes for the generator and the Python tools. The most common are unquoted `yes` / `no` / `on` / `off` (previously booleans, now strings); a key tagged `!!merge` (previously merged) makes the whole file unreadable. Once the exporter goes through the shared judgement, it also stops serving files `parse-tree` judges unreadable (§2).

**Guarding an upgrade**: two things, both on the customer's own tree, with the new version's tools:

- **What the new version refuses**: validate your conf.d in CI with the new da-guard. The files the new da-guard reports with exit 3 are the files the new exporter does not serve and the generator and tenant-api refuse (all three use the same judgement, §2, §9); the report names the `kind` of each.
- **How the output differs**: render the same tree once with the old and once with the new da-tools image and compare the rendered results (the Alertmanager routing config and so on), the same approach as [ADR-037](./037-config-diff-compares-rendered-output.en.md)'s config-diff.
- The cost: the comparison shows where the rendered result changed, but not which change in reading caused it; to attribute it, look at the source text at that spot.
- The release notes list the affected spellings and the migration steps.
- The step that deletes the migration audit command also adds these two pre-upgrade checks (validating conf.d with the new da-guard, comparing the rendered output of the old and new images) to the [incremental migration playbook](../scenarios/incremental-migration-playbook.en.md).

**The migration audit command retires**: it reads the customer's tree with two parsers and lists the differences, but the side it takes as "today" is PyYAML plus a loader imitating yaml.v3, a reading that never shipped in any release (the released `tools/v2.9.0` has no such loader). It answers "how does an unreleased reading differ from the new one", not the customer's question, "what does the new version refuse, and how does its output differ"; it is also the last place the Python side reads conf.d with PyYAML. So the two things above replace it: until the step that deletes it, the command is still there but should not be relied on; that step removes it (§7).

Deleting the vendored yaml.v3 "non-specific tag" patch changes nothing further in how the exporter reads: by then the exporter and tenant-api decode only files `parse-tree` judges readable, and a file holding a `!`-tagged node such as `! "<<"` or `objective: ! "99.9"` is unreadable as a whole, not served like any other unreadable file, and listed under the new da-guard's exit 3.

### 9. da-guard

da-guard gains `parse-tree` and stops judging routing and policy; the generator's `--validate --strict` judges them. Threshold checks are unchanged.

- **When da-guard runs alone** (for example the standalone binary without Python):
  - The report (text and JSON) states that routing and policy were "not judged", and returns exit code 4, so "not judged" and "judged, no problem" can be told apart.
  - It returns 4 only when the tree contains content that only the generator can judge: routing, policy, or `_` keys that only the generator reads or, misspelled, only the generator reports (how they are judged: [ADR-038](./038-unread-keys-judged-by-readers.en.md)). A tree without any still returns 0.
  - When several exit codes apply, the precedence is 3 > 1 > 4 (3 is files that cannot be read, 1 is error-level findings; 3 over 1 is unchanged from today).
  - The one definition of what exit 3 covers is [the guard section of the CLI reference](../cli-reference.en.md#guard): files the exporter drops whole when it loads, files that fail to decode while merging (the `_defaults.yaml` chain or a tenant file), and files, directories or paths that cannot be read. Once the exporter goes through the shared judgement, it does not serve files `parse-tree` judges unreadable (§2), so all of them are reported with exit 3, for example a BOM in the middle of the file, a tag like `!!binary`, or more than one document. The class of exit 4's "not judged" evidence for "`parse-tree` treats it as unreadable while the exporter reads it" (`generator_unreadable`) is no longer needed and folds into exit 3: the generator and the exporter refuse the same files. Only paths this run's exit 3 cannot name, yet that make the generator refuse the whole tree, stay "not judged" evidence (`unreadable_outside_run`): such files outside `--scope`, a directory named like a config file or a symlink to a directory, and a tree that cannot be scanned at all; the guard section of the CLI reference defines it.
  - Exit 4 stays for routing, policy and `_` keys only the generator judges. Its condition is still a prediction of whether the generator judges the tree; if almost every real tree returns 4, the condition carries no information, and whether to keep it is re-evaluated then.
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

| Item | Along with | Action |
|---|---|---|
| Small utilities unrelated to parsing (for example the tenant id check, finding which directory level a file sits at) | `parse-tree` shipping | move to another package |
| The rest of tenant-api's Go-side routing and policy judging: the checks on a write's routing patch and receivers, judging a tenant's routing against policy, the old behaviour of treating an unreadable file as empty | tenant-api asking the generator container | delete |
| Go code that imitates PyYAML's reading: the PyYAML-decoding imitation package, the function imitating PyYAML's type resolution, the "spaces only" option the policy decoder turns on | once neither tenant-api nor da-guard uses it | delete |
| Go code that imitates the generator's semantics (the Go copy of routing and policy judging, including deciding whether `_routing` is a string that turns routing off) | same | delete |
| The Go copy of the receiver contract (which receiver types exist, which fields each requires) | the judging part: with the row above | delete the judging part; the exporter only uses it to look up whether a receiver type exists, and that part stays |
| The vendored yaml.v3 "spaces only" patch (refusing TAB indentation; only the policy decoder above turns it on) | with the rows above | delete |
| The divergence catalog, its tests and the expiry check in `make pre-tag` | with the rows above | delete |
| Python loaders, imitating yaml.v3, used to read conf.d; the Python copy of the file-selection rules | the generator and the Python tools reading the JSON | delete |
| da-guard's "not judged" evidence `generator_unreadable` | the exporter and da-guard going through the shared judgement | folded into exit 3; paths this run cannot name become `unreadable_outside_run` (§9) |
| tenant-api's own conf.d write shape rules | tenant-api using the shared judgement | replaced by `parse-tree`'s judgement |
| The vendored yaml.v3 "non-specific tag" patch (keeping YAML's `!` marker on the node of a quoted string, so `! "<<"` is a merge key) | once the exporter and tenant-api both go through the shared judgement | deleted (step 8): files with `!` never reach their decoding, and the patch has no effect (§7, §8) |
| The vendored yaml.v3 duplicate-key change (duplicate-key checking of large mappings via a hash table instead of O(n²) pairwise comparison) | — | keep: it guards against a denial of service that stalls decoding with very many keys; it does not imitate another parser |
| The vendored yaml.v3 unhashable merge key fix (a non-scalar key beside a merge key becomes a decode error instead of a runtime panic) | in parallel with the rest | patch deleted: the vendored base moved to upstream `go.yaml.in/yaml/v3` v3.0.5, which carries the same fix; the duplicate-key change is laid on top of the new base. Before moving, the old and new bases were compared with the fixed corpus (`parse_tree_corpus.json` and the yaml.v3 reading tests); the readings do not differ |
| The 4 Go file-name rules; the Python name predicate and the table comparing Go and Python name rules | the file-name rules converging | Go: merged into one; Python: deleted, using the list-only mode instead |
| The layer in the conf.d-writing tools that reads originals and produces text with PyYAML, with the loader imitating yaml.v3 it depends on | once every tool that writes conf.d sends "what to change" | delete (§2) |
| The migration audit command | the pre-upgrade checks going into the incremental migration playbook | delete (§8) |

The order of the items is in §7; the code for each is in the appendix. CI's declaration table records when each package and patch may be deleted; the PR of each step that deletes code updates it.

Duplicate keys, more than one document, tags outside the allow-list: the generator's loader and the Go imitation package used to block these (some Python tools deliberately read only the first document). Before the Go checks are deleted, `parse-tree` must already treat these as unreadable (§2), confirmed by the fixed corpus; that is why `parse-tree` ships first (§7).

### 12. YAML outside conf.d: its consumer decides

What a YAML file means is decided by the program that reads it and acts on it. When a Python tool needs the content of such a file, it asks for that consumer's reading instead of reading it with PyYAML of its own; neither PyYAML nor yaml.v3 is the authoritative reader of these files.

| File | Authoritative reader | How the Python side gets it |
|---|---|---|
| Kubernetes manifests, Alertmanager config | kubectl (sigs.k8s.io/yaml), Alertmanager (yaml.v2), both following YAML 1.1's rules | decoded by `da-crdecode` (decodes with sigs.k8s.io/yaml and prints JSON), made general-purpose; it must support more than one document per file |
| The admin config tenant-api reads (for example RBAC, groups, views) | tenant-api's loader (`parse-tree`'s judgement first, then a yaml.v3 decode, §2) | read the way tenant-api reads it; the mechanism is open and decided in the step for YAML outside conf.d (for example a validation subcommand in tenant-api) |
| conf.d examples in the docs | the exporter and the generator (through `parse-tree`) | `parse-tree`'s whole-tree mode |
| Files with no Go consumer that only Python reads in the repo (GitHub workflows, pre-commit config, mkdocs config, repo-internal data) | — | not enforced; unchanged |

Example: `name: no` in a Kubernetes manifest is the boolean `false` to kubectl and the string `"no"` to `parse-tree`. Judging the manifest by yaml.v3's reading would disagree with kubectl, which actually applies it; so ask sigs.k8s.io/yaml.

**The boundary and how it is held**: if a file has a Go consumer, Python must not read it with PyYAML. CI runs an AST lint (parsing the Python source's syntax tree to find where a YAML parser is called) that lists every call site; the file holding a call site must be on an allow-list, each entry with its reason (for example "reads only GitHub workflows; no Go consumer").

Scope: the order of this section's work is in §7. The portal's js-yaml previews stay as §2's scope note says and are not covered here.

## When things fail

| Situation | Behaviour |
|---|---|
| The generator's exit code on the whole tree after the write is not 0 | Refused; the error response lists the findings, marking those that existed before the write and their source |
| The generator container is unavailable: the long-running process fails or does not respond, or amtool is missing | Every write that changes file content gets 503, error code `VALIDATOR_UNAVAILABLE`; `tenant_api_policy_available` is 0 (§5) |
| The judging deadline passes (including while queued), or a child exceeds its memory limit or produces incomplete output, or the remaining time is not enough to revalidate | Only that write is refused, no 503 (the line is drawn in §3) |
| The deployment policy file is unavailable | Every write that changes file content gets 503, error code `POLICY_UNAVAILABLE` |
| A policy file inside the repo has an error | The generator reports a finding; refused as in the first row, not with 503 |
| `parse-tree` cannot read a file | The file is listed under unreadable files, the generator's check does not pass, and the write is refused |
| The body of a tenant-api write, or the file a whole-file write replaces, is judged unreadable | The body: 400, the error naming the `kind` and the line, nothing written; the replaced file: counts as one that does not parse, a caller without write permission on every tenant gets 400 |
| Admin config tenant-api reads itself is judged unreadable | Each reader's existing parse-failure handling, never read as empty (§2): the RBAC config and `_tenant_orgs.yaml` fail startup and keep the previous version on a hot reload |
| While the exporter loads, a file is judged unreadable (once the exporter goes through the shared judgement) | As for a syntax error in the same kind of file today (a tenant file: its tenants are not served; a `_` platform file: skipped whole, read as empty in the defaults chain, §2); a metric and a log name the file and the `kind`; da-guard reports the same file with exit 3 |
| Text written by da-guard's `write` reads back through `parse-tree` differently from what was expected | Nothing is written, and the differing value is reported |
| A reader meets an unknown `schema` value | The whole document is treated as unusable |
| The generator in the container is older than a policy requires | Every write that changes file content gets 503, error code `VALIDATOR_UNAVAILABLE`, until the operator raises the chart's da-tools version |
| No da-guard locally and no Go to build it | Python tools that read or write conf.d and the pre-commit hooks fail with the reason; they never fall back to parsing or writing on their own. The affected hooks and the CI jobs without Go are listed by code in the implementation PR; those CI jobs switch to the da-tools image or install Go |

## Known limits and risks

Risks that go away once the Go side has one judgement and one writer and YAML outside conf.d follows its consumer:

- The exporter reading a file the generator refuses (for example the exporter silently dropping the tenants of a second document).
- The conf.d-writing tools dropping comments when they rewrite with PyYAML, and the patches keeping it reading like yaml.v3 missing case after case.
- The file-name rules disagreeing between several Go and Python implementations.
- The migration audit using a reading that never shipped as its baseline, misleading customers about an upgrade's impact.

Risks that are new or remain:

- **The upgrade risk of the exporter refusing files**: once the exporter goes through the shared judgement, a file read today may not be served at all, and the alerts of the tenants in it stop. A customer who did not validate conf.d with the new da-guard before upgrading finds out after the upgrade. The only mitigations are §8's upgrade guard and the metric and log; there is no switch to "read as before".
- **The cost of the judgement, and its cache**: every file is judged once more before it is decoded. The exporter keeps verdicts by the SHA-256 of the file content (at most 8,192; when full the set is dropped whole, with one DEBUG log line), so the same content is not judged twice; a cold start still judges every file once (measured: a cold scan of 1,000 tenant files takes 109 ms, 70 ms with every verdict kept). A process that meets more distinct contents than the bound keeps dropping the set, its hit rate falls towards 0 and the cost goes back to the uncached figure; the verdicts do not change. An unreadable file's message is bounded to 512 bytes where `parse-tree` makes it: a longer one keeps its head (the line number and what was refused) and its tail (a repeated key's "first at line M", for one), the middle omitted — so the exporter's log, tenant-api, da-guard's reports and the `parse-tree` JSON carry the same text.
- **yaml.v3's output format**: once the tools that write conf.d write through Go, blank lines, the alignment of end-of-line comments and the quoting style change, and customers' diffs show format differences unrelated to the change itself (§2).
- **The cache stays**: the generator container keeps caching parse results across requests, because the conditions for removing it do not hold (§3 "Whether the cache stays"). The cost is that the cache, its eviction rules and the cache-size metric must be maintained; the cold start fills it with the batch single-file mode, in time that grows linearly with the number of files.
- **`da-crdecode` does not support more than one document per file yet**: Kubernetes manifests in §12 often hold several, and until it does there is no authoritative reading to ask for such files.
- **A writing change brought by the new yaml.v3 base**: when the base moved to upstream v3.0.5, the old and new bases were compared with the fixed corpus and the readings do not differ. Writing differs in one place: when a string that starts with a line break is written as a literal block (`|`), the old base left out the leading blank line, so it read back without that line break; the new base writes it. Two places in tenant-api change: when it merges custom alerts, such a string used to be written double-quoted and is now a literal block, and both read back as the same string; a partial update over a block scalar that opens with a blank line used to be refused (`not_round_trippable`) because the re-encode changed it, and now the re-encode keeps it and the write goes through. A later upstream upgrade is compared with the fixed corpus first in the same way (§11).
- **The rendered-output comparison cannot attribute**: §8's comparison says where the result changed, not which reading caused it.

## How it is accepted

- **No second YAML parser**: a CI check lists every YAML loader call site on the Python side and allows only the parse-result JSON; once the migration audit command is deleted, conf.d has no exceptions. Files outside conf.d that have a Go consumer are held by §12's AST lint and its per-file allow-list with reasons.
- **One judgement on the Go side**: for every row of the fixed corpus that `parse-tree` judges unreadable, the exporter does not serve it, tenant-api refuses to write it and da-guard returns exit 3; for every row it judges readable, all three read it.
- **One writer**: each `write` operation (rewrite, create, replace, move, delete) has tests: nodes and comments outside the edit are written back as they were, and a read-back mismatch writes nothing.
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

Every measurement in this section was taken before the generator read `parse-tree`, while it read files with PyYAML; samples after the switch are in §3, "Whether the cache stays".

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

This is the basis for §3's long-running process with a parse cache; whether it stays now that the generator reads `parse-tree` is decided by the measurement in §3, "Whether the cache stays".

Syntax layer (`parse-tree`, sample measured 2026-10-20; a synthetic tree of 1000 files with one tenant each):

| Item | Measurement |
|---|---|
| `da-guard parse-tree` reading the whole tree once | about 0.12 s, about 858 KB of output |
| Python reading that JSON | about 0.04 s |

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
| `parse-tree` refuses everything the generator used to refuse | It would mean rewriting every PyYAML check in Go, once again having Go imitate the other parser; comparing the two sides kept finding new "only PyYAML refuses" classes. Instead the list is closed and everything else is read per yaml.v3 (§2) |
| A two-phase switch (old and new side by side, compare differences, delete the Go side once conditions hold) | The product has few adopters and can take breaking changes; the side-by-side period means maintaining two judges and a divergence catalog, and Go's judging is known to be looser, so keeping it buys nothing |
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
| The exporter only observes and does not refuse: it keeps reading files `parse-tree` judges unreadable and only adds a metric and a log | That keeps two judgements (what the exporter reads and what the generator reads), and a file can still be served by the exporter and refused by the generator — the "each reads its own way" this ADR removes |
| Read all YAML with yaml.v3 (files outside conf.d through `parse-tree` too) | The wrong dialect for Kubernetes manifests and Alertmanager config: their authoritative readers follow YAML 1.1's rules, so `name: no` is `false` to kubectl and `"no"` to yaml.v3 |
| Draw the boundary at `import yaml` (every file importing PyYAML is controlled) | Too wide: files that only read GitHub workflows and the like, with no Go consumer, would be blocked too; and easy to get around: calling through a wrapper function hides `import yaml`. Instead an AST lint finds the call sites, with a reason per file (§12) |

## Implementation order

- Done: the generator outputs structured findings.
- Done: CI checks (users of the deletion scope, the divergence catalog and its two-way tests). The divergence catalog was deleted in step 3.
- Done, step 0 (ship first, no behaviour change): `parse-tree` and the fixed corpus `tests/shared/parse_tree_corpus.json`; the migration audit command; moving the utilities unrelated to parsing.
- Done, step 1: the prerequisites listed in §7; the generator container (validation endpoint, status endpoint); the Helm chart adds the container, socket, policy-file mount and resources; tenant-api's writes ask the generator container, deleting tenant-api's Go-side routing and policy judging, the flat key `_routing_receiver_type` and `--policy-unavailable-open` at the same time.
- Done, step 2: da-guard adds the "not judged" state and exit code 4; `guard-defaults-impact.yml` also runs the generator.
- Done, step 3: deleted the Go code that imitates PyYAML and the generator's semantics, the vendored yaml.v3 "spaces only" patch, the divergence catalog and the expiry check in `make pre-tag`.
- Done, step 4: the generator and the Python tools read the parse-result JSON; deleted the Python imitation loaders used to read conf.d; the tools that write conf.d added the pre-write check. The "non-specific tag" patch moves to step 8.
- Done, step 5: `parse-tree`'s judgement moves to a shared package under `pkg/`.
- Done, step 6: the exporter, da-guard and tenant-api's read side pass conf.d through it when loading; the exporter does not serve unreadable files and adds a metric and a log; `generator_unreadable` folds into exit 3, and paths this run cannot name become `unreadable_outside_run`.
- Done, step 7: tenant-api's write shape rules use it; so do its reads of `_tenant_orgs.yaml`, the RBAC config, groups, views, the federation whitelist and subsets, and the AccountID registry.
- Done, step 8: deleted the "non-specific tag" patch; the exporter's merge-key check no longer accepts the `!` marker.
- In progress, step 9: done — the Go file-name rules converged into one, `parse-tree`'s list-only mode, one per-path index in every mode with Go's `role` verdict (`da-guard.parse-tree/v2`), and the Python tools that already depended on da-guard reading `role` (a tool that already holds the whole-tree document reads its `entries` instead of listing again); and the Python name rules are deleted (step 9b-3): `_lib_confd`'s name predicates (`is_reserved_name`, `is_hidden_name`, `has_yaml_extension`, `is_defaults_name`, `select_defaults_carrier`, `config_stem` and the like) are gone together with the tests that compared them with Go, and their former call sites read Go's data instead — a path in a tree reads the index's `role`, `class`, `tenant_named`, `not_read` and `defaults_carriers`; a name that is not in a tree (a ConfigMap key, a file name not yet written) asks `parse-tree`'s names mode, which also answers whether a name carries a config file's extension (`config_extension`, hidden names included) and which defaults carrier those names, put in one directory, select (the others get `not_read: unselected_defaults_carrier`); a Go test requires names mode and tree mode to agree on every row of the name table. The file-name table (`tests/shared/confd_name_classification_matrix.json`) is owned by Go's `pkg/confdname` test alone, which checks every cell and the table's coverage floor. `tests/shared/test_confd_tools_no_python_judgement.py` now keeps those predicates from coming back. Not yet done: what the gate cannot see, guarded instead by the behaviour matrix (`tests/shared/test_confd_behaviour_matrix.py`). Its trees come from a catalogue that a Go test requires to hold a tree for every role and problem kind Go can write; the matrix runs the listing-level trees among them plus two content errors (a YAML syntax error, a file holding several documents), and the other content errors are checked by the Go test only. Every tool runs as nobody, judged by da-guard's validation exit on the same tree: on 0 the tool must exit 0; on 3 or 4 it must exit non-zero, without a traceback or a hang, and name every path the validation report lists; on a tree holding a file name that is not UTF-8 it must likewise exit non-zero (which code is the tool's documented contract: `tenant_verify`'s 2 means a verification mismatch, so it exits 1), without a traceback or a hang, naming the file in its escaped form. The conf.d tools share one fail-closed exit: when the run's whole-tree index has any entry whose `verdict` is not null, every such path gets an `ERROR:` line and the run ends with exit 2 before anything is written; the tools whose verdict on the tree is their output — `validate-config`, the route generator, `check_routing_profiles`, `check_confd_schema`, `config_diff` — do not take that exit and name such paths and fail themselves; a tool that reads one subtree of conf.d (`deprecate_rule --plane subtree`) takes the same exit: its index is the conf.d root's whole tree with the subtree's part cut from it, because `parse-tree` run on a subtree alone judges it as a root. A cell that does not hold is listed on its own as an expected failure; there are none now. `config_history`, `drift_detect` and `run_chaos_soak` take every file they read from that index, so all of them need da-guard; `migrate_to_operator`'s `--source-dir` holds ConfigMap dump files, not a conf.d, so its files come from `parse-tree`'s list-only mode and it does not take this exit. Outside the matrix, `batch_diagnose` filters ConfigMap dump files by name and does not take the exit yet either; it moves to it in the next step (β2). What it guards: `describe_tenant`'s `--replaces` and `check_path_metadata_consistency` judging conf.d paths by stat or prefix. `offboard_tenant` now tells a tenant's own file by the tenant id its name carries (`tenant_named`), not by a file-name prefix. The generator service, when it writes a git tree to disk (`_gsvc_child`), now asks names mode which path component is hidden; the route generator and the routing-profile lint, telling whether a platform file's tenant is declared by anyone, now read both halves of `TenantScope`, so an entry that may hide a tenant file is named in the message instead of a declared tenant being called an orphan.
- In progress, step 10: done — the edit primitives as a shared package (`pkg/confedit`, the §4 invariant included), which tenant-api's batch patch and custom-alerts merges now use, and da-guard's `write` (rewriting, creating and moving a batch of files at once), which writes them itself after the read-back; the three tools that change existing conf.d files (`patch_config`, `threshold_govern`, `deprecate_rule`) now send what to change to `da-guard write`, and one that changes several files sends them all in one call, judged as a batch, written only when every one passes. Of the tools that create new files, `generate_tenant_fixture`, `onboard_platform`, `migrate_rule`, `scaffold_tenant` and `init_project` now hand the data to write (typed values and comments) to `da-guard write` all at once, the whole tree judged once, written only when every file passes (the last four replace a file already there whole when rerun into the same output directory). Not done yet: the other tools that create or move files (`migrate_conf_d`, `da_assembler`) still produce the text in Python and write it after the pre-write check.
- Step 11: delete the Python layer that rewrites conf.d with PyYAML and the imitation loader it depends on.
- Step 12: YAML outside conf.d follows its consumer; `da-crdecode` made general-purpose with multi-document support; the AST lint and its allow-list.
- Step 13: delete the migration audit command; the incremental migration playbook gains the pre-upgrade checks.
- In parallel: `parse-tree` streaming its output and the measurement for "Whether the cache stays".
- Done, one of the parallel items: moved the vendored yaml.v3 base to upstream v3.0.5 and deleted the unhashable merge key patch.

The steps merge into main in order, and main must be releasable after each one (§7).

## Appendix: code locations

Where the code is, for implementers; can be skipped when reading the decision. Code comments that say "ADR-036 step 2" (or 「第 2 步」) mean the CI checks in the implementation order; 「實作順序第 4 步」 means tenant-api asking the generator container.

| Term in this document | Code |
|---|---|
| The existing "Python calls a Go binary and reads its JSON" mechanism | `scripts/tools/_lib_tenant_values.py` (`served-values` / `effective`) |
| The Python YAML-reading and file-selection libraries (switch to `parse-tree`) | `scripts/tools/_lib_io.py`, `_lib_confd.py`, `_lib_yaml_keys.py` |
| Python loaders that imitate yaml.v3 | `_lib_yaml_keys.ExporterKeyLoader`, the `StrictExporterKeyLoader` family in `_lib_io`, `_GoKeyLoader` in `describe_tenant` (deleted: describe-tenant reads `da-guard effective`); `ExporterKeyLoader` and the `StrictExporterKeyLoader` family are still used by the writing tools' rewrite layer and go with it |
| The PyYAML rewrite layer of the tools that write conf.d (retires) | `StrictRewriteLoader`, callers of `strict_load_for_rewrite` / `load_yaml_file_for_rewrite` in `_lib_io` and `_lib_yaml_keys.dump_for_rewrite` (for example `patch_config`, `deprecate_rule`, `da_assembler`); `scaffold_tenant` and the others that create new files with `yaml.safe_dump`; the `_lib_yaml_keys.ExporterKeyLoader` they depend on |
| The Python pre-write check (moves into Go) | `scripts/tools/_lib_write_check.py` |
| The edit primitives (a shared package) | `pkg/confedit`: node edits (`Doc`'s `Set`, `Unset`, `EnsureMap`), how values are written (`StringNode`, `BlockStringNode`, `DropImplicitTags`), §4's invariant (`Verify`); extracted from tenant-api's `internal/customalerts/merge.go` and the batch patch's `mergePatchYAML`, which now call it. da-guard's `write` is in `cmd/da-guard/write.go` |
| `parse-tree`'s judgement | `pkg/parsetree` |
| The Go file-name rules (merged into one) | `pkg/confdname`. The other copies are deleted and their sites call it: the extension check and `hasHiddenSegment` in `pkg/config/source.go`; `IsScannedFileName`, `IsScannedPath` and the walk's inline prefix checks in `pkg/config/tree_scan.go`; `TenantIDFromFile` and `isReservedName` in tenant-api's `internal/confd`; the hand-written extension check in `findingReadable` (tenant-api `handler/errors.go`); `hiddenPath` in da-guard's `parse_tree.go` |
| `parse-tree`'s mode that lists files without parsing them | `da-guard parse-tree --list-only` (`cmd/da-guard/parse_tree_list.go`); the walk is `ScanDirTreeNames` in `pkg/config` |
| The per-path index and the `role` verdict | `entries` are built by `walkEntries` in `cmd/da-guard/parse_tree_list.go` (shared by the whole-tree and list-only modes); the verdict is `pkg/config/role.go` (`UnreadableFile.Role`, `NotFile.Role`); the Python reader is `scripts/tools/_lib_parse_tree.py` (`Entry`, `entries_of`, `list_tree`, `classify_names`) |
| The Python name predicate (deleted) and the name-rule comparison table (now Go's alone) | the name checks that were in `scripts/tools/_lib_confd.py` (for example `is_reserved_name`, `config_stem`) and the Python tests comparing them with the table; the table, `tests/shared/confd_name_classification_matrix.json`, stays as the fixture of Go's tests (`pkg/confdname`'s matrix test owns every cell and the coverage floor) |
| The precedent of decoding with sigs.k8s.io/yaml (made general-purpose) | `cmd/da-crdecode` (refuses a file with more than one document today) |
| The migration audit command (retires) | `scripts/tools/ops/migration_audit.py` |
| Policy file names the generator reads on its own | `_POLICY_FILENAMES` in `_grar_parse.py`; the exporter skipping nested `_` files is explained in `flat_build.go` |
| The portal's preview-only parsing | js-yaml via `tools/portal/src/interactive/tools/_common/validation/yaml-parser.js`; the hand-written line-by-line parsing `tools/portal/src/interactive/tools/config-lint/lint.js` |
| Utilities unrelated to parsing, moved out | `IsValidTenantID` (callers use `pkg/tenantid`), `LevelOf` and `IsDomainPolicyFile` (`internal/confdlayout`); the routing-profiles file names are `routingProfilesNames` in `cmd/da-guard` |
| Code that blocks duplicate keys and multiple documents | Python's `RejectDuplicateKeys`; on the Go side `parse-tree` (`pkg/parsetree`), which da-guard reads for its undetermined evidence. The Go copies (`pyyamlcompat.FindDuplicateKey`, `firstDocument` / `errMoreDocuments` in `routingpolicy`, da-guard's `pyyaml_duplicates.go`) are deleted |
| Go code that imitates PyYAML's reading (deleted) | `pkg/pyyamlcompat`; `pyResolve` and the policy decoder that turned on `SpacesOnly` in `pkg/routingpolicy` |
| Go code that imitates the generator's semantics (deleted) | `shapeWork`, `ParseDomainPolicies`, `UnmarshalPolicy`, `IsDisabled` in `pkg/routingpolicy`; da-guard's routing checks `internal/guard/routing.go` and their finding kinds |
| Changes in the vendored yaml.v3 | `third_party/yaml.v3`: spaces only (deleted), non-specific tag (deleted), duplicate-key checking of large mappings (keep), the unhashable merge key fix (deleted: the base is upstream `go.yaml.in/yaml/v3` v3.0.5, which provides it); the pinning test `tests/ops/test_vendored_yaml_v3.py` |
| A file with `!` never reaches the exporter's decoding | `pkg/config/nonspecific_tag_test.go`; the merge-key check `isMergeKey` in `pkg/config/config_file.go` |
| The Go copy of the receiver contract | `pkg/receiverspec`: `Known` (used by the exporter's routing resolution; kept); `Check` (deleted) |
| tenant-api's current write judging | the generator container's caller `internal/generator` (`Gate`); on the Go side only the shape and reserved-key checks remain (next two rows) |
| tenant-api's entry to the shared judgement | `Judge` in `internal/confd/verdict.go` (returns `pkg/config`'s `UnreadableFileError`); callers: `validateShape`, `eolGuardErrs`, the parse functions of `tenantorg`, `rbac`, `groups`, `views`, `fedpolicy` and `account`, and the batch's `tenantAbsentIn` and `scopeBlockOnDisk` |
| Go re-emitting batch patches | `mergePatchYAML`; the invariant after the merge `verifyPartialWrite` (`handler/tenant_list.go`; the check is `confedit.Verify`); the offline differential test `handler/batch_patch_diff_test.go` |
| The rest of tenant-api's Go-side routing and policy judging (deleted) | the routing-patch check `validateRoutingPatch`; the receiver check `receiverViolations` in `gitops` (`placeholderViolations` in the same file is the masked-credential write-back guard and stays); `JudgeTenantBlock` / `CheckTenantRouting` in `internal/policy`; `tenantBlockOnDisk`, which treats an unreadable file as empty |
| tenant-api's non-routing checks that stay | `validateShape` in `gitops/writer.go` (`parse-tree`'s judgement first, then a yaml.v3 check that the exporter can read the content); the reserved-key value check (`_silent_mode`) is now made by the write gate with the exporter's own reader (`CheckReservedValue` in `pkg/config`, [ADR-038](038-unread-keys-judged-by-readers.en.md) decision 12), and `handler/body_validator.go` keeps only the length caps and the key checks the reserved-key registry decides |
| Descriptions to update for da-guard's exit 4 | the exit-code comment in `cmd/da-guard/main.go`; `scripts/tools/_lib_exitcodes.py`; `docs/cli-reference.md` |
| The deletion-scope user check (CI) | declaration table `tests/shared/phase2_deletion_scope.json`; the test comparing it, `components/threshold-exporter/app/phase2_deletion_scope_test.go` |
| Checks only Go's da-guard had (moved to the generator) | the generator's side is pinned by `tests/shared/routing_policy_parity_matrix.json` |
| The divergence catalog (deleted; the files and target below no longer exist) | `tests/shared/reader_divergence_catalog.yaml`; the Go verdict snapshot `tests/shared/merge_key_go_verdicts.json`; `tests/shared/test_reader_divergence_catalog.py`; `reader-divergence-expiry` in `make pre-tag` |

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
