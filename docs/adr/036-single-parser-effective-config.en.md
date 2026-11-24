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

✅ **Accepted** (2026-10-09). **Implemented**: every step of §7 is merged into main.

Revisions (each is folded into the sections in parentheses; practices a later revision replaced are not listed):

- 2026-10-11: Python reads the JSON Go parses whenever it judges config content; a write lands only when the whole tree after it passes the generator; the "let writes through when the policy is unavailable" switch is gone; tenant-api requires git; `parse-tree`'s output, the closed "unreadable" list and da-guard's "not judged" exit code are defined (§2, §5, §9).
- 2026-10-12, 14, 15: the generator container caches parse results across requests (keyed by the SHA-256 of the file content), and each judgement runs in a child process with memory and time limits; batches are all or nothing; an entry fetched from git that is not a regular file is unreadable; a batch patch is held to an invariant on the merged result (§3, §4).
- 2026-10-16, 30: "not judged" also covers `_` keys only the generator judges; a container that returns no key verdict counts as unavailable (§3, §9, with [ADR-038](./038-unread-keys-judged-by-readers.en.md)).
- 2026-10-20, 21: the Go side also keeps one judgement and one writer (the exporter, tenant-api and da-guard share one judging package; conf.d is written only through `da-guard write`); every `parse-tree` mode prints the same per-path index (`da-guard.parse-tree/v2`) (§2, §7, §11).
- 2026-10-22, 23: the vendored yaml.v3 is rebased on upstream `go.yaml.in/yaml/v3`; after measuring, the parse cache stays and a cold start reads in batches (§3, §11).
- 2026-10-28 to 11-01: YAML outside conf.d follows its consumer: Kubernetes manifests and Helm values through `da-crdecode`, Alertmanager config, Prometheus rule files and `prometheus.yml` through `da-read`, tenant-api's admin config through its own `state show` / `config check` (§12).
- 2026-10-30: the migration audit command is deleted; the pre-upgrade checks are in the incremental migration playbook (§8).
- 2026-11-13 to 15: whether the external programs of a judgement (da-guard, da-read, da-crdecode) ran is answered by the judgement itself, and one that did not run is a 503 `tool_fault`; readiness rests on the three programs' built-in self-checks; the container's responses carry only closed codes and structured fields (§3, §5).
- 2026-11-15: the version gap once the platform-default thresholds are built into the readers is recorded (§6).
- 2026-11-21: Python reads YAML one way only, the specification's strict reading, customer files included (§12).
- 2026-11-22: doc examples gain one more fence attribute, `{.quote src="…"}` (§7 step 11).
- Revised 2026-11-24: rewritten to describe current behavior; the decision is unchanged.

## Decision

**A conf.d (the tenant config directory) is read by one YAML parser only: Go's yaml.v3.** Any other program that needs to know what a file says reads the JSON Go prints instead of parsing it. The semantics have one implementation each, too: routing, domain policy and the tenant list are judged by the route generator (Python); thresholds and the values the exporter actually serves are judged by the exporter (Go).

1. **Only Go parses YAML.** `da-guard parse-tree` reads a whole conf.d into one JSON: what each path is, who reads it, whether it can be read, and what each readable file holds. The Python tools (the route generator included) read that JSON (§2).
2. **The Go side has one judgement.** The exporter, tenant-api and da-guard all load conf.d through the package that produces this JSON; a file it judges unreadable is refused by all three (§2). The file-name rules have one implementation, in Go.
3. **conf.d has one writer.** Rewriting an existing file and creating a new one are done by `da-guard write`; Python tools only send "what to change" (§2).
4. **tenant-api asks the generator before every write that changes file content.** The tenant-api pod gains a generator container; a write lands only when the whole tree after it passes the generator's `--validate --strict` (§3, §4). When the generator container or the deployment's policy file is unavailable, the write gets a 503; there is no switch that lets it through (§5).
5. **YAML outside conf.d: its consumer's reader when it can be called, otherwise a strict reading by the YAML specification** (§12).

Example. In this tenant file neither the key `010` nor the value `yes` is quoted:

```yaml {.confd .expect-invalid}
# t010.yaml
tenants:
  010:
    _routing:
      receiver:
        type: slack
        api_url: {secret: {key: slack-api-url}}
        send_resolved: yes
      group_wait: 30s
```

The `files` that `da-guard parse-tree --config-dir conf.d` prints (excerpt):

```json
{"t010.yaml": {"tenants": {"010": {"_routing": {
  "group_wait": {"text": "30s", "tag": "!!str", "value": "30s"},
  "receiver": {
    "api_url": {"secret": {"key": {"text": "slack-api-url", "tag": "!!str", "value": "slack-api-url"}}},
    "send_resolved": {"text": "yes", "tag": "!!str", "value": "yes"},
    "type": {"text": "slack", "tag": "!!str", "value": "slack"}}}}}}}
```

The tenant id is the string `"010"` and `send_resolved` is the string `"yes"` (PyYAML would read the octal integer 8 and the boolean `True`). The generator, the exporter and tenant-api all see this one reading.

### 1. Who parses what

| Layer | The one implementation | How other tools get it |
|---|---|---|
| YAML syntax: which files are selected, what each holds, which cannot be read | Go's `parse-tree` package (yaml.v3) | Python reads `da-guard parse-tree`'s JSON; the exporter, tenant-api and da-guard call the same package in process |
| conf.d file-name rules (which names are tenant files, which tenant id a name maps to) | Go (one implementation) | Python reads `parse-tree`'s per-path index |
| Writing conf.d: rewriting existing files, creating new ones | Go (`da-guard write`) | Python tools send "what to change" |
| Semantics of routing (`_routing_defaults`, `routing_profiles`, a tenant's `_routing`), domain policy, the tenant list | The route generator (Python) | Read the findings it prints (each problem it reports) and the generator container's status endpoint (§5) |
| Thresholds and the values the exporter actually serves | The exporter (Go) | Read the JSON of `da-guard served-values` / `effective` |
| YAML outside conf.d that another program reads | That consumer | Its reading, when its reader can be called; otherwise a strict reading by the specification (§12) |

The thresholds row has long been "one implementation, other tools read its output"; this ADR applies the same approach to the syntax layer, the file-name rules, writing, and the routing and policy semantics.

### 2. The parse-result JSON

`da-guard parse-tree` has four modes, all printing the same kind of document:

| Mode | Command | Use |
|---|---|---|
| Tree (`tree`) | `parse-tree --config-dir <dir>` | The index of every path, plus the content of every readable file |
| File (`file`) | `--path <rel> --input <file\|->`; in batches `--inputs` | Judge a text that is not on disk yet, before writing it |
| List (`list`) | `--list-only --config-dir <dir>` | Only the list of files, no content read |
| Names (`names`) | `--list-only --names <file\|->` | Classify by name only, for names not on a disk (e.g. a ConfigMap's keys) |

**The envelope**: `schema` is `"da-guard.parse-tree/v2"`, with `da_guard_version`, `mode`, `defaults_carriers` (the one `_defaults` file each directory uses) and `entries`; tree and file mode also carry `files`, `rendered_by` and `config_hash`. A reader that meets any other `schema` value treats the whole document as unusable. A new field does not change `schema`; only a change to a shape readers rely on does, as with `served-values` / `effective`.

**One entry per path**: `entries` has one entry for every path the exporter's walk records, and every entry writes every field, `null` where there is no value. Tree and list mode are built by the same code; they differ only in the problems that reading the content finds.

- `path`, `depth`, `class` (`tenant`, `defaults`, `platform`, `directory`), `tenant_named` (the tenant id the file name carries), `defaults_document`, `config_extension`.
- `readers`: who reads it (`exporter`, `generator`); an empty list when nobody does, with the reason in `not_read` (e.g. `unselected_defaults_carrier`: the spelling not selected when both exist in one directory).
- `role`: what this path is; readers take it as given and do not combine fields themselves. `config` is a config file, readable or not, with the reason in `problem` when it is not; `dir` is a directory the walk enters; `not_a_file` is a place where the exporter cannot reach a regular file (a symlink to a directory, a FIFO, a dangling or looping link); `unlistable_dir` is a directory whose contents could not be listed, which means the document is incomplete. Only the cases listed make a path `not_a_file`; any other failure (permission denied, EIO) is `config`, so a tenant file whose stat fails does not vanish from the list.
- `problem`: why reading it fails, `{kind, message}`.
- `verdict`: how a da-guard validation without `--scope` lists this path: `unservable` (listed under exit 3: the exporter cannot serve it), `tree_refused` (listed under exit 4: the generator refuses the whole tree over it) or `null`. A reader that must fail on every path validation names reads this field. It answers per path only: `null` everywhere does not mean validation passes (a tenant declared in two files, for example).

Example: a tree with `t1.yaml`, both `_defaults.yaml` and `_defaults.yml`, a `team/t2.yaml` with a syntax error, and `gone.yaml`, a symlink to a file that does not exist. `da-guard parse-tree --list-only --config-dir conf.d` prints (three entries of it):

```json
{"path": "_defaults.yml", "depth": 0, "class": "defaults", "tenant_named": null, "defaults_document": true, "config_extension": true, "readers": [], "not_read": "unselected_defaults_carrier", "role": "config", "problem": null, "verdict": null}
{"path": "gone.yaml", "depth": 0, "class": "tenant", "tenant_named": "gone", "defaults_document": false, "config_extension": true, "readers": ["exporter", "generator"], "not_read": null, "role": "not_a_file", "problem": {"kind": "read_failure", "message": "the path cannot be statted (stat_error)"}, "verdict": "unservable"}
{"path": "team/t2.yaml", "depth": 1, "class": "tenant", "tenant_named": "t2", "defaults_document": false, "config_extension": true, "readers": ["exporter", "generator"], "not_read": null, "role": "config", "problem": null, "verdict": null}
```

Tree mode gives the same three entries, except that `team/t2.yaml` gains `"problem": {"kind": "syntax_error", "message": "line 2: yaml.v3 cannot parse the text into a document"}` and its `verdict` is `"unservable"`.

**The content of each file**: the structure as yaml.v3 decodes it.

- The top level must be a mapping; an empty file is an empty mapping.
- Aliases (`*x`, pointing back to what `&x` marks) and the unquoted merge key `<<` (merging another mapping in) are expanded; a quoted `"<<"` is an ordinary key. Readers need not expand anything.
- Mapping keys are always printed as their source text (`010` is `"010"`, the way the exporter reads a tenant id).
- A scalar is printed as its source text, the type yaml.v3 resolves, and its converted value, e.g. `{"text": "0777", "tag": "!!int", "value": "511"}`. Booleans and nulls are JSON `true` / `false` / `null`; integers and floats are decimal strings, so precision does not differ between the two languages; timestamps and infinities carry only the text and the type.
- The semantic layer judges only by the type and the converted value, never by the loose conversion yaml.v3 applies when decoding into a typed Go field (the string `yes` decoded into a bool field gives `true`).

**Unreadable files**: `problem.kind` is a closed list of 13; `files` has no content for such a file. The list holds only spellings the readers could read with different meanings:

| `kind` | Case |
|---|---|
| `read_failure`, `not_regular_file`, `invalid_utf8`, `syntax_error` | The read fails, not a regular file, not valid UTF-8, a syntax error |
| `top_level_not_mapping` | The top level is not a mapping (an empty file is not this) |
| `decode_error` | Everything yaml.v3 reports while decoding, e.g. alias expansion past the limit, `!!int abc`, `<<: 1` |
| `multiple_documents` | More than one document in a file (separated by `---`). yaml.v3 reads only the first; copying that would silently drop whatever follows the `---` |
| `duplicate_key` | A key repeated in one mapping (compared by key text after aliases are expanded) |
| `null_key`, `non_scalar_key` | A null key (`~:`, `null:`, an empty key), or a key that is not a scalar (`? [a]`) |
| `tag_not_allowed` | A tag outside the allow-list. Scalars take only `!!str`, `!!int`, `!!float`, `!!bool`, `!!null`, `!!timestamp`; `!!binary`, `!!set`, `!!merge`, `!foo` and the like are unreadable |
| `non_specific_tag` | YAML's non-specific tag `!` (`! "x"`, `! "<<"`): reading it as a merge key or as a literal key can each silently add or drop policy |
| `bom_not_at_start` | A BOM (U+FEFF, an invisible character marking the encoding) anywhere but the start of the file: it can put invisible characters into a key or a value |

Anything outside the list is read as yaml.v3 reads it, including spellings PyYAML refuses and yaml.v3 reads: one anchor name defined twice, a date that does not exist (`2001-13-45`), a TAB after a colon. Why `parse-tree` does not refuse everything PyYAML refuses is under "Alternatives considered".

Example. A file holding two documents:

```yaml {.confd .expect-refused=multiple_documents}
# t1.yaml
tenants:
  t1:
    mysql_connection_usage_percent: "80"
---
tenants:
  t2:
    mysql_connection_usage_percent: "90"
```

`parse-tree` judges it `multiple_documents` ("a second document starts at line 4; only one document per file is read"), with `verdict` `unservable`; the exporter serves neither `t1` nor `t2`; `da-guard --config-dir conf.d` exits 3 naming `t1.yaml`. The fix is to move the second document into a file of its own.

**The Go readers follow this judgement too**: the judgement is a Go package (`pkg/parsetree`) that the exporter, tenant-api and da-guard all import. Each of them asks it before decoding a readable file into its own structures, so the generator, the exporter, da-guard and tenant-api refuse the same files.

- An unreadable tenant file in the exporter: the tenants it declares are not served, other tenants of the tree are unaffected, a WARN is logged and `da_config_parse_failure_total` goes up by one. An unreadable `_defaults` or other `_` platform file: skipped whole with one ERROR line ("entire block dropped"), merged into the defaults chain as an empty file, and listed by `da-guard effective` under `chain_parse_failed`; thresholds served only because of that file's defaults are no longer served.
- A tenant-api write whose body is judged unreadable gets a 400 whose text names the `kind`. When the original a whole-file write would replace is unreadable, it is treated as an original that cannot be parsed: replacing it takes write permission on all tenants.
- The admin config tenant-api reads itself (`_tenant_orgs.yaml`, the RBAC config, groups, views, the federation allow-list and its subsets, the AccountID ledger) is judged first too. When unreadable it gets that reader's existing parse-failure handling, never "an empty file": these are mostly inputs at an authorization boundary, and an empty RBAC is a different authorization, not "no config". A failed hot reload of the RBAC config, for example, keeps the previous one.
- da-guard exits 3 on these files (§9).

**How the Python side gets it**: Python runs the Go binary and reads JSON (`scripts/tools/_lib_parse_tree.py`, the same mechanism as for `served-values` / `effective`). When a tool that reads conf.d gets the whole tree's index and any entry has a non-null `verdict`, it names each such path in an `ERROR:` line and exits 2 before writing anything. The generator uses the JSON's type and value everywhere it uses a value.

**Writing conf.d files: Go is the only writer.** Python tools decide "what to change" and hand it to `da-guard write`:

- One call is one batch and may mix rewriting existing files (set, unset, ensure-map), creating, replacing whole, moving, copying in from outside the tree, and deleting; the batch is judged once and written only when all of it passes.
- A rewrite edits yaml.v3's node tree and replaces only the edited nodes; every other node and comment is written back as it was (§4's merge invariant applies too). A whole replacement does not read the old file, so it can also repair a file `parse-tree` cannot read.
- What is judged is the whole tree after the write: each file must read back as expected, and the write must not make the tree worse — another file losing its reader, any path newly showing a state validation names, or a new error in da-guard's validation stops the whole batch; a problem already present elsewhere does not block an unrelated write.
- A delete can carry the precondition "this file declares exactly these tenants"; when it does not hold, the whole batch is not written.

Consequence: the output takes yaml.v3's style. Blank lines may collapse, and the alignment of trailing comments and the quoting of written values change; but more comments survive than a rewrite through PyYAML kept.

**Scope**: this section covers conf.d only; other YAML is §12. The portal's in-browser tools parse with js-yaml for preview only; their result is never what lets a write or a CI run through.

### 3. How it runs (tenant-api)

**The tenant-api pod gains a generator container (the da-tools image, `da-tools generator-service`), and tenant-api asks it before every write.**

```text
tenant-api pod
┌──────────────────────────────┐      ┌──────────────────────────────┐
│ tenant-api (Go)               │ ───▶ │ generator container (da-tools)│
│ - receives the write request  │ check│ - fetches files at a commit   │
│ - writes or refuses by result │ ◀─── │ - da-guard parse-tree parses  │
│ - reads status for metrics    │result│ - generator judges semantics  │
└──────────────────────────────┘      └──────────────────────────────┘
          the two talk only over a Unix socket in a shared directory
```

In the container a resident process fetches files from git and caches each file's parse result across requests; each judgement runs in a child it forks (a copy of the process made by the operating system, starting with the resident process's memory, so it inherits the cache), finishing within a memory limit and a judgement deadline, one at a time. Trouble in the child refuses only that write; only trouble in the resident process makes the container unavailable.

- **Communication**: HTTP/1.1 over a Unix socket, no network port; the socket is in a shared scratch directory in the pod (an emptyDir).
- **Cache**: the key is (the SHA-256 of the file content, the parse mode). Changing the content changes the key, so stale content is never read. Not git's blob SHA: SHA-1 collisions can be crafted, letting the parse result of refused content be used to judge other content. Content in the latest base commit's tree is always kept; other content (e.g. from a refused write) is capped at 32 MiB in total, least recently used dropped first. The cache holds content only; which files are selected and whether they can be read is recomputed for every judgement, and the deployment's policy file, not being in git, is re-read every time.
- **Whether the cache stays**: the cache holds Go's output, not a second reader, so keeping it is a performance decision. The condition for dropping it: on a 5000-tenant tree, every program of one full judgement (`parse-tree`, the generator, `da-read alertmanager`, and the served-values, effective and `key-verdict` the key verdict runs) peaks at no more than 75% of the child's memory limit, and the judgement takes no more than half the deadline. Measured (`scripts/tools/dx/measure_judgement_budget.py`, numbers in [#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)) it does not hold, so the cache stays. On a cold start the files that miss go to `parse-tree --inputs` in batches, one walk of the tree per batch, so the time grows linearly with the number of files. Tree mode's output was not streamed when this was measured; to reconsider dropping the cache, stream it first and measure against the same condition.
- **Deadlines**: one request's total deadline is tenant-api's HTTP write timeout (`--write-timeout`, default 30 seconds); waiting for the write lock, asking the generator, re-judging and committing all fit inside it. tenant-api passes the time left with every call; the container's judgement deadline is the smaller of "20 seconds" and "the time left minus 5 seconds", queueing included; the 5 seconds cover delivering the response and tenant-api's check and commit. When it expires, the resident process answers "refusing that write". The child is its own process group (`setsid`), ended as a whole with `killpg` at the deadline, so no da-read or da-guard is left behind.
- **Resources**: the container's request equals its limit, 512Mi memory and 1 CPU. Each judgement child also gets `RLIMIT_DATA` 192 MiB (Linux's limit on one process's writable memory; the cache inherited by fork counts), and each Go program it starts inherits the same limit. One judgement runs at a time, so the worst case is about 192 + 192 + roughly 80 for the resident process, under 512. Not an address-space limit (`RLIMIT_AS`): the Go runtime reserves a lot of virtual address space at start, and a low limit keeps the programs from starting.

```yaml {.quote src="helm/tenant-api/values.yaml"}
  # ADR-036 §3: request = limit (512Mi, 1 CPU). The memory budget is the
  # resident service (~80Mi) plus one judgement process and one Go program it
  # starts (192Mi RLIMIT_DATA each). The render fails when requests and limits
  # differ; raise both together (and the service's --memory-limit-bytes) when
  # the parse cache outgrows the judgement budget.
  resources:
    requests:
      cpu: "1"
      memory: 512Mi
    limits:
      cpu: "1"
      memory: 512Mi
```

**Where "refusing that write" ends and "the container is unavailable" begins**: when the resident process answers and the answer names this write's result, it is "refusing that write", and the container stays available; counting these as an unavailable container would let one write turn every later write into a 503.

| Case | What tenant-api returns |
|---|---|
| The generator has a blocking finding | 422 `GENERATOR_FINDINGS` |
| The child exceeds its memory limit, the output is incomplete, the files this write produces are too large, git attributes would commit other bytes than the ones judged | 422 `GENERATOR_REJECTED` (reason `memory`, `incomplete`, `too_large`, `normalized` and so on); resending the same write does not get past it |
| The judgement deadline expires, the container is busy, the git read fails, a re-judgement still does not match, too little time is left to judge or to re-judge | 429 `GENERATOR_BUSY` (reason `deadline`, `busy`, `git_read`, `mismatch`, `no_time`); may be resent later |
| The generator passed, but the key verdict ([ADR-038](./038-unread-keys-judged-by-readers.en.md) decision 5) did not complete | reason `key_verdict_*`: out of time (`key_verdict_no_time`, `key_verdict_deadline`) is 429 `GENERATOR_BUSY`, the rest (`key_verdict_memory`, `key_verdict_error`, `key_verdict_platform_file`) 422 `GENERATOR_REJECTED` |
| The resident process does not answer or its answer cannot be read, a program of the judgement did not run, the generator is older than the policy requires or the image gives no key verdict, the container's own error | 503 `VALIDATOR_UNAVAILABLE` (reason `unreachable`, `bad_response`, `tool_fault`, `generator_too_old`, `internal_error`) |
| The deployment's policy file is unusable | 503 `POLICY_UNAVAILABLE` (§5) |

The container's responses carry only closed codes and structured fields, no free text; the details go to the container's own log.

**The programs of a judgement must run**: one judgement runs three Go programs: da-guard, da-read (the Alertmanager and Prometheus config loaders, §12) and da-crdecode (reads manifests the way kubectl does). If any of them did not run, that write has no verdict and gets a 503 `tool_fault`, whatever the generator concluded without it.

- "Did not run" is the program itself failing: not found, not executable, timed out, ended by a signal, an exit code or output outside its contract. The program's answer about its input is not that: da-guard judging a file unreadable or da-read refusing a config is that write's result as usual.
- da-read and da-crdecode exit 2 on a refusal as on a usage error; standard output tells them apart: only a refusal prints the fixed refused document `{"answer":"refused"}`, with the reason on stderr. Exit 2 without that document means the program did not run. So the Python tools and these two programs must be the same version. ⚠️ The v2.9.0 image's da-crdecode does not print the refused document yet (that image has no da-read either); paired with newer Python tools, every refusal reads as the program not having run <!-- image-caveat: v2.9.0 -->.
- A program the Go runtime ends itself because of the judgement's memory limit is not a fault: that write is refused with reason `memory`.
- **Readiness**: the container reports ready (`ready` in `GET /v1/status`) only when the policy is usable, the generator is not older than the policy requires, the three programs' built-in self-checks have all passed, and no tool fault is still uncleared. A self-check is each program's own command (`da-guard self-check`, `da-read self-check`, `da-crdecode --self-check`): it runs every subcommand once on samples packed in the binary and, on success, prints `{"self_check":"passed"}` with exit 0. As soon as a judgement meets a tool fault, the container reports not ready and runs a round of self-checks; only a passing self-check clears the fault.
- Known limit: a self-check runs one input per subcommand, while judgements use more flags, so a judgement can still meet a tool fault after the self-check passed; that request then gets a 503 and the container goes back to not ready until the next round.

**Fetching files: the generator container takes files from git objects only, never the working tree.**

- It mounts the repo tenant-api uses read-only and takes the files of a given commit with `git ls-tree` and `git cat-file`. Not the working tree: PR mode switches branches in place, and a read could see a half-switched state. Not `git archive`: it applies `.gitattributes` export rules, so a policy file a customer marks as not exported would be invisible to the generator.
- Only regular files (mode 100644, 100755). A symlink, a gitlink (a submodule) or any other entry is judged `not_regular_file` and blocks the write: a symlink read from git objects is only the link's text, and judging that text gives a result that depends on what the text happens to look like. Hidden paths (a segment starting with `.`) are not judged. This is stricter than readers of the file system: the exporter and customer CI follow symlinks (every key of a mounted ConfigMap is one), so the same tree may be green in customer CI while tenant-api refuses the write; the fix is a regular file with the same content.
- The two containers run as the same uid: git refuses to read a repo another uid owns. The repo turns off automatic garbage collection (`gc.auto=0`), so objects are not pruned while one side commits and the other reads.
- git is required: deployments without git are not supported.

### 4. How writes are validated

**After the write, the generator checks the whole tree; the write lands only if everything passes.**

- **Criterion**: the generator container puts the new content in place of the files being written and runs `--validate --strict` on the whole tree after the write; exit 0 and only then is it written. It uses the same base commit, the same deployment policy file and the generator version in the container. The criterion is the generator's own result, so tenant-api is never looser than the generator in its container.
- **Which writes are checked**: every write that changes file content, including one that changes only thresholds; Go does not decide whether routing was touched.
- **Error response**: lists the findings the generator reports, those about the written files first, marking which already existed before the write. This affects the message only, not the verdict.
- **Batches**: one request, one commit, all or nothing. The whole batch is applied and written only if the whole tree then passes; otherwise the whole batch is refused. Synchronous and asynchronous batches follow the same rule. Each batch is applied, judged and committed in its own temporary git worktree, then the branch ref is advanced and the serving working tree updated; if advancing is interrupted, the batch's paths are reset to the branch's content at the next start or before the next write. Readers never see content that was never committed.
- **Merging a batch patch**: Go reads the file, applies the patch and writes it back, and the merged content is always checked. The merge must satisfy an invariant, or it is refused with `TENANT_CONFIG_NOT_LOADABLE` (`config_error` `not_round_trippable`): parse both the original and the merged result into yaml.v3 node trees; outside what this edit replaces, every node must write out the same (kind, style, tag, scalar text, anchors and the node an alias points to). The same characters mean the same thing to every reader, so this criterion does not depend on any reader. Values the merge writes are double-quoted, so no reader takes them for a time, a number or a boolean; the one exception is a value under `_custom_alerts` that holds a line break and reads back as itself when written as a literal block (`|`): it is written as a literal block, so a diff shows it line by line.
  - The error names the path of the first difference only inside the caller's own tenant block, and says only "elsewhere" otherwise: one file may hold several tenants. A decode failure returns fixed text; the parser's own text goes only to the server log.
  - `PUT …/custom-alerts` refuses with 409; in a batch it is that item's code and the whole batch gets a 422.
  - An offline diff test (`components/tenant-api/internal/handler/batch_patch_diff_test.go`) also holds the invariant to the generator's own reading: one side is Go's merge read by the generator, the other the generator reading the original with the same patch laid over it, with the file's other tenants compared too.
- **What is checked must be what is written**: the container returns the file paths, content hashes, base commit and deployment policy hash it judged; tenant-api compares them under the write lock. On a mismatch it judges once more, and refuses if it still does not match (429 `mismatch`). The bytes written must be exactly the bytes judged.
- **PR mode checks the branch's starting point**: what is checked is the base commit of the PR branch plus this write. The result after the PR merges into the trunk is outside tenant-api's verdict; customer CI or a merge queue guards it.

**Cost**: while anything in the tree blocks, every write through tenant-api is refused until it is fixed. When the problem is in one tenant file and fixing it makes the whole tree pass, it can be fixed through tenant-api; otherwise (a platform file, several tenants in violation at once) through git. When customer CI uses the same da-tools version with the same deployment policy file, a change that turns the tree red never reaches the trunk, so the trunk stays green.

### 5. Policy files and policy status

**The deployment's policy file is mounted into the generator container; when it is unusable, writes are refused.**

- **The deployment's policy file** (`--policy`): its `allowed_domains` restrict webhook URL domains, an SSRF control (stopping the service from being used to send requests into the internal network on an attacker's behalf), so it lives outside the tenant repo where someone with write access to the repo cannot loosen it. The Helm chart's `generator.policy` mounts it read-only into both the generator container and the tenant-api container (tenant-api checks the hash with it, §4). The customer CI `da-tools init` generates can bring in the same file (GitHub's variable or GitLab's File-type CI variable `DA_TOOLS_POLICY`), so CI sees on the pull request the answer tenant-api will give; this side is not a security control. Setup: [GitOps CI/CD Integration Guide §2.5](../scenarios/gitops-ci-integration.en.md#25-bringing-in-the-deployment-policy-file-da_tools_policy).
- **Status endpoint**: the generator container reports whether the deployment's policy file is usable, and tenant-api turns it into `tenant_api_policy_available`; when the container cannot be reached, the metric is 0. The alert `TenantApiPolicyUnavailable` reads it. An error in a policy file inside the repo does not make it 0; that is a generator finding, refused as in §4.
- **No switch to let writes through**: while the deployment's policy file or the generator container is unavailable, every write that changes file content gets a 503 (`POLICY_UNAVAILABLE` or `VALIDATOR_UNAVAILABLE`, §3). Users who can send writes can affect whether the container is available, and falling back to a looser judgement would let the requester pick its own judge.
- **No tidied config JSON is produced**: no program needs to read it.

### 6. Versions

Which da-tools version a customer's CI uses has nothing to do with tenant-api, so "exactly matching the customer's CI" is impossible. What can be guaranteed: **tenant-api is never looser than the generator in its container**. da-guard and the generator are in the same da-tools image, so their versions are decided together.

When the two sides differ, either side refusing keeps the config out of Alertmanager: when customer CI is stricter, a write tenant-api accepted is stopped in CI; when the container is stricter, tenant-api refuses a write CI would accept. Neither lets violating config through; the cost is that the two verdicts disagree.

This does not cover the platform-default thresholds: their values are compiled into the readers ([ADR-017](017-defaults-yaml-inheritance-dual-hash.en.md) decision 11), so when the checkers (da-guard in CI, tenant-api) are newer than the running exporter, a tree relying on built-in values passes the checker while the exporter does not serve those rows. That lets something through; whether the two sides agree shows in whether the digests of their built-in tables match.

Narrowing the version gap:

- tenant-api's chart pins one da-tools version (`generator.image.tag`); the config `da-tools init` generates pins a version too, never `latest`.
- The deployment's policy file can declare `min_generator_version: "X.Y.Z"`: while the generator in the container is older, every write that changes file content gets a 503 (reason `generator_too_old`); the fix is raising the chart's da-tools version. The CLIs that judge receivers with the same `--policy` (`validate-config` and others) refuse to judge too when older. The key is read only from the deployment's policy file.
- tenant-api's `/metrics` passes on the generator container's metrics, among them `da_generator_build_info` with the generator's and da-guard's versions.

### 7. Merge in order on main

**The steps are merged into main in order, and main can be released after each.** No period of old and new side by side, no switch. The ordering principle: the Go code imitating PyYAML was deleted before the generator moved to yaml.v3's reading, so no released commit has "the generator already reads with yaml.v3 while Go still imitates PyYAML". Code comments cite the step numbers, so they are kept.

| Step | What |
|---|---|
| 0 | `parse-tree` and its fixed corpus `tests/shared/parse_tree_corpus.json` ship first, behavior unchanged; small utilities unrelated to parsing move out of the packages to delete. Repeated keys, multiple documents and tags outside the allow-list were partly stopped by Go's imitation code, so `parse-tree` had to judge them unreadable before that code was deleted |
| 1 | tenant-api writes ask the generator container; tenant-api's Go routing and policy judgement, the flat key `_routing_receiver_type` (writing it is now a 400) and the let-through switch are deleted. Checks that are not routing judgement stay: the guard against writing back a masked-secret placeholder, the check that the exporter can read what is written, and the value checks of reserved keys. Prerequisites: `da-tools init` pins its version, git writes commit several files at once with all-or-nothing batches, the generator container and its chart settings |
| 2 | da-guard stops judging routing and policy and reports "not judged" (exit 4, §9); `guard-defaults-impact.yml` also runs the generator |
| 3 | The Go code imitating PyYAML and imitating the generator's semantics is deleted (§11). From this step on, no Go program produces a routing or policy finding |
| 4 | The generator and the Python tools read the parse-result JSON; the Python loaders that imitated yaml.v3 to read conf.d are deleted |
| 5 | `parse-tree`'s judgement moves to a shared package under `pkg/` |
| 6 | The exporter, da-guard and tenant-api's read side load conf.d through this judgement; the exporter does not serve unreadable files. A breaking change users can see; the pre-upgrade guard is in §8 |
| 7 | tenant-api's writes and the admin config it reads itself go through this judgement |
| 8 | The vendored yaml.v3 "non-specific tag" patch (it made `! "<<"` a merge key) is deleted. Only after steps 6 and 7: by then a file holding `!` reaches no component's decoder, so deleting the patch silently changes no file any component reads |
| 9 | Go's file-name rules become one (`pkg/confdname`); every `parse-tree` mode prints the same per-path index, and Python reads `role` instead of deciding itself whether a path is a config file |
| 10 | The shared package of edit primitives (`pkg/confedit`) and `da-guard write`; every Python tool that writes conf.d sends only "what to change" |
| 11 | The Python layer that rewrote conf.d with PyYAML is deleted. YAML examples in the docs are judged by one inventory (`scripts/tools/_lib_doc_yaml_inventory.py`); the author declares a fence's reader with an attribute such as `{.confd}`, `{.k8s}`, `{.alertmanager}`; input unreadable on purpose is `{.confd .expect-refused=<kind>}`, a counter-example wrong on purpose `{.confd .expect-invalid}`, a block no check judges `{.illustrative reason="…"}`, and an excerpt of a repo file `{.quote src="…"}` (compared line by line with its source by the quote guard). How to write them is in the [doc template](../internal/doc-template.md) |
| 12 | YAML outside conf.d follows its consumer, with the lints that hold the boundary (§12) |
| 13 | The migration audit command and the Python loader imitating yaml.v3 are deleted; the incremental migration playbook gains the pre-upgrade checks (§8) |

Done alongside: the measurement for "whether the cache stays" (§3); rebasing the vendored yaml.v3 on upstream v3.0.5 (§11). Not done: streaming tree mode's output from `parse-tree`, needed only to reconsider dropping the cache.

Customers' conf.d trees are not available, so the checks before each merge covered only the trees in the repo; how a customer sees the impact on their own tree is §8.

### 8. Migration: knowing the impact before upgrading

With yaml.v3 parsing, every value the two parsers read differently changes. The most common are unquoted `yes` / `no` / `on` / `off` (booleans before, strings now); a key tagged `!!merge` makes the whole file unreadable. The exporter also does not serve files `parse-tree` judges unreadable (§2).

Before upgrading, do two things on your own tree with the new tools; how is in the [incremental migration playbook](../scenarios/incremental-migration-playbook.en.md), "Checks before upgrading the platform":

- **What the new version refuses**: validate your conf.d with the new da-guard. The files it exits 3 on are the ones the new exporter does not serve and the generator and tenant-api refuse (all three use one judgement); the report lists them file by file.
- **Where the output differs**: render the same tree once with the old and once with the new da-tools image and compare the rendered output, as [ADR-037](./037-config-diff-compares-rendered-output.en.md)'s config-diff does. It shows where the result changed, not which change of reading caused it.

The migration audit command is deleted: its "today" side was PyYAML plus a loader imitating yaml.v3, a reading no released version ever had, so it could not answer what a customer asks — what the new version refuses and where the output differs.

### 9. da-guard

da-guard has `parse-tree` and `write` and no longer judges routing and policy, which go to the generator's `--validate --strict`. Threshold checks are unchanged.

- When the tree holds what only the generator judges (routing, policy, or `_` keys only the generator reads, judged as in [ADR-038](./038-unread-keys-judged-by-readers.en.md)), the report says "not judged" and exits 4, so "not judged" and "judged, no problem" can be told apart; a tree without such content exits 0 as before.
- When several exit codes apply, the order is 3 > 1 > 4 (3: a file cannot be read; 1: an error-level finding).
- The one definition of what exit 3 covers is [the guard section of the CLI reference](../cli-reference.en.md#guard), including every file `parse-tree` judges unreadable. Exit 4's evidence also lists paths this run cannot name while the generator would refuse the whole tree over them (`unreadable_outside_run`): unreadable files outside `--scope`, a directory named like a config file, and so on.
- `--required-fields _routing.*` is an error with exit 2, never a silent no-op.
- No flag lets CI declare "routing already judged": the customer CI `da-tools init` generates already runs `generate-routes --validate --strict`. A CI that runs only da-guard needs to add that step.

Example: for a tree with one tenant file carrying `_routing` and nothing else wrong, `da-guard --config-dir conf.d` exits 4 and the last line on stderr is:

```text
da-guard: routing and domain policy not judged: 1 item(s) only the route generator judges — run `da-tools generate-routes --config-dir conf.d --validate --strict` — da-guard no longer judges routing or domain policy (ADR-036 §9) (exit 4)
```

### 10. tenant-api read endpoints

- No GET endpoint returns routing as the generator renders it. When someone needs it, `GET /tenants/{id}/routing` is added, its content produced by the generator container.
- The `_routing` in the `/effective` response is the raw key as the exporter merges it, not rendered routing.

### 11. What gets deleted

Decided by "is anyone still using it". CI lists the users of the packages involved against a declaration table (`tests/shared/phase2_deletion_scope.json`) and goes red when they disagree.

| Item | Outcome |
|---|---|
| Go code imitating PyYAML's reading (`pkg/pyyamlcompat`, the policy decoder's "spaces only" option) | Deleted |
| Go code imitating the generator's semantics (Go's routing and policy judgement, da-guard's routing checks) | Deleted |
| tenant-api's Go routing and policy judgement, and the old behavior of treating an unreadable file as empty | Deleted |
| Go's receiver spec | The judging part is deleted; the exporter uses it only to check that a receiver type exists, and that part stays |
| The catalog of where Go and Python read differently | Deleted |
| Python loaders imitating yaml.v3, the file-selection rules, the name predicates, the layer that rewrote conf.d with PyYAML | Deleted; replaced by reading `parse-tree` or sending to `da-guard write` |
| Go's several sets of file-name rules | Merged into `pkg/confdname` |
| The vendored yaml.v3 "spaces only" and "non-specific tag" patches | Deleted |
| The vendored yaml.v3 fix for unhashable merge keys | The base is upstream `go.yaml.in/yaml/v3` v3.0.5, which carries the same fix; the patch is deleted |
| The vendored yaml.v3 change to duplicate-key checking (a hash table instead of pairwise comparison in large mappings) | Kept: it guards against a denial of service by a huge number of keys, not an imitation of another parser |
| The migration audit command | Deleted (§8) |

### 12. YAML outside conf.d: ask its consumer when you can, otherwise read strictly by the specification

A YAML file means what the program that reads it and acts on it takes it to mean. When that consumer's reader can be called, the Python tools ask it; neither PyYAML nor yaml.v3 is the authoritative reader of these files. A file with no reader to call is read strictly by the YAML specification, without copying where a consumer is looser than the specification.

| File | Authoritative reader | How Python gets it |
|---|---|---|
| Kubernetes manifest | kubectl (sigs.k8s.io/yaml, YAML 1.1 rules) | `da-crdecode` (the manifest reading) |
| Helm values file | Helm (sigs.k8s.io/yaml) | `da-crdecode --any-shape` |
| Alertmanager config | Alertmanager's own config loader (upstream `config.Load`) | `da-read alertmanager` |
| Prometheus rule file | Prometheus' loader (upstream `rulefmt.Parse`) | `da-read prometheus-rules` |
| Prometheus config (`prometheus.yml`) | Prometheus (upstream `config.Load`) | `da-read prometheus` |
| tenant-api's state records (groups, views, the federation allow-list and its subsets, the AccountID ledger) | tenant-api's parse function for each record | `tenant-api state show` |
| The RBAC config and `_tenant_orgs.yaml` tenant-api reads | Same as above | `tenant-api config check` |
| conf.d examples in the docs | The exporter and the generator | `parse-tree` (through §7 step 11's inventory) |
| Files with no Go consumer (GitHub workflows, the pre-commit config, the mkdocs config, the repo's internal data) | — | The strict reading (the second boundary below) |

Example: in an Alertmanager config, `match: {tenant: 010}` is the string `"010"` to `da-read alertmanager` (Alertmanager decodes it into a string field with yaml.v2, taking the text); the same text read the Kubernetes way is the number 8 (YAML 1.1 reads a number starting with 0 as octal). So first ask who reads the file.

**`da-read`**: calls the consumer's own loader and prints the loaded config, keeping only keys the text writes (keys not written and defaults filled in are not printed). When the loader refuses, or a key the text writes does not match the loaded result, it refuses and names the path. `da-read` is a Go module of its own; the Prometheus version it compiles in matches the deployed Prometheus image, held by a version-pin test. `da-read prometheus` imports upstream `discovery/install` as Prometheus does, registering every built-in service-discovery config type; otherwise fields such as `kubernetes_sd_configs` would be refused. vmalert reads the same rule files, but rulefmt's reading is the authority; whether vmalert can read them is left to the existing vmalert-tool test (`tests/rulepacks/test_vm_alert_parity.py`).

**`da-crdecode`'s two readings**: the same conversion (sigs.k8s.io/yaml, documents split as `kubectl apply -f` splits them), differing in what shape each document must have.

- **manifest (the default)**: every document must be an object with a `kind`, and its `apiVersion` and `metadata` must decode into the types kubectl reads them as (Kubernetes apimachinery's `TypeMeta` and `ObjectMeta`); the check calls that same library, so it accepts and refuses exactly what kubectl does. A list (a document with a top-level `items`) is replaced by its items as kubectl does; an item writing neither `kind` nor `apiVersion` inherits both from the list (an item of a `ConfigMapList` is a `v1` `ConfigMap`). Where it is stricter than kubectl: items get the type check too (kubectl does not check them when reading the file, but such an item is refused or altered when applied); a list carrying content of its own next to `items` is refused (kubectl drops it without a word); and when any document is refused, nothing of the file is printed.
- **`--any-shape`**: reads YAML that is not a manifest (Helm values, a standalone rule file, a string value in a ConfigMap); it converts and requires no shape.

The output carries a `reading` field naming the reading, and per document a `differs_from_text` (the keys and strings whose Kubernetes reading is not their text). A Python caller that gets a `reading` other than the one it wants treats it as unreadable.

```text
$ da-crdecode list.yaml          # kind: List, two ConfigMaps
{"documents":[{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"a"}},{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"b"}}],"differs_from_text":[[],[]],"reading":"manifest"}
$ da-crdecode cmlist.yaml        # kind: ConfigMapList, the item writes only metadata and data
{"documents":[{"metadata":{"name":"a"},"data":{"k":"v"},"apiVersion":"v1","kind":"ConfigMap"}],"differs_from_text":[[]],"reading":"manifest"}
$ da-crdecode values.yaml        # replicaCount: 1 (exit 2; the first line is stderr, stdout is the refused document)
da-crdecode: values.yaml: it is not a Kubernetes object: it has no `kind` (kubectl: Object 'Kind' is missing)
{"answer":"refused"}
$ da-crdecode --any-shape values.yaml
{"documents":[{"replicaCount":1}],"differs_from_text":[[]],"reading":"any-shape"}
```

**`tenant-api state show` and `config check`**: tenant-api's own subcommands, calling the same parse functions the server uses (`parse-tree`'s judgement first, then decoding, then the record's own checks). `state show` reads the state store the server is configured with, or a backup with `--from`, and prints `tenant-api.state-show/v1`: every record has exactly one of `record` (the decoded result) or `problem`. `config check --kind rbac|tenant-orgs` judges an RBAC config or a `_tenant_orgs.yaml` and prints `tenant-api.config-check/v1`. Python tools (for example `offboard_tenant`'s `--state` and `check_admin_config_schema`) read these JSONs; a record a tool uses that carries a `problem` counts as undetermined, never as empty.

**The first boundary: Python must not read a file with a Go consumer through PyYAML.** An AST lint (`tests/shared/test_yaml_reader_boundary.py`, which parses Python's syntax tree to find calls to a YAML parser) lists every call site; each must be on the allow-list (`tests/shared/_yaml_reader_baseline.py`) with its reason, and the allow-list takes only readers with no Go consumer. Call sites with a Go consumer not yet migrated go on a separate baseline that only shrinks; it is empty now.

**The second boundary: Python reads YAML one way only, the specification's — the keys of a mapping must be unique.** The one reading is `_lib_yaml_strict`, which refuses a repeated key; PyYAML by default keeps the last one without a word. `tests/shared/test_yaml_strict_only.py` scans every tracked Python file in the repo and the Python embedded in other files, and fails on any reference to a PyYAML or ruamel.yaml reading entry outside `_lib_yaml_strict`; there is no list of exceptions. Tests are not scanned: they read fixtures they wrote, and the strict reading's own tests need the lax reading as their control. Customer files are no exception: even when the consumer keeps the last key (GitLab reading `.gitlab-ci.yml`, for one), a repeated key is an error reported to the customer to fix.

```yaml {.illustrative reason="A repeated key on purpose: init's .da-init.yaml, which the strict reading refuses"}
# .da-init.yaml (init's record of the files it wrote)
files:
  ci/a.yml: h1
files:
  ci/b.yml: h2
```

With PyYAML's default reading the first `files:` vanishes, and init would take `ci/a.yml`, which it wrote, as not its own; the strict reading refuses the file, and init names the repeated `files` and deletes nothing.

## Background

The same conf.d used to be read twice, by two YAML parsers: yaml.v3 on the Go side (the exporter, tenant-api, da-guard) and PyYAML on the Python side (the route generator and the tools). The two often read the same file differently. Take a key tagged `!!merge` in a tenant's `_routing`:

```yaml {.confd .expect-refused=tag_not_allowed}
# t1.yaml
tenants:
  t1:
    _routing:
      !!merge q: {receiver: {type: slack, api_url: {secret: {key: slack-api-url}}}}
```

PyYAML treats it as a merge, so t1's receiver is `slack`, and the generator reports an error when domain policy forbids slack; yaml.v3 treats `q` as an ordinary key, so the receiver falls back to the platform default and da-guard judges the config compliant. One file, two tools, opposite conclusions — and the side letting it through was Go. Now `parse-tree` judges this file `tag_not_allowed`, and every reader refuses it.

Gaps of the same kind:

| Spelling | PyYAML | yaml.v3 |
|---|---|---|
| `send_resolved: yes`, `on`, `off` | boolean | string |
| `a: "x\/y"` (`\/` in a quoted string) | `x/y` | the whole file unreadable |
| `a:` followed by a TAB and then the value (`a:<TAB>"v"`) | the whole file unreadable | read normally |
| More than one document in a file | `safe_load`: the whole file unreadable | only the first read, the rest silently ignored |

Aligning them meant one side rewriting every path of the other's parser in its own language: Go had a package imitating PyYAML and patches to the vendored yaml.v3, Python had several loaders imitating yaml.v3. Each fix exposed another gap next to it; feeding random input to both and comparing finds gaps but cannot prove the two equal. Restricting conf.d to spellings both must read the same way did not work either: the values in the second and third rows above are quoted and use no advanced feature, and the two still read them differently.

Converging the semantics on the generator alone was not enough: with the generator still reading through PyYAML and Go through yaml.v3, the syntax-level gaps remain. The Go side also had more than one judgement: of 9 measured spellings `parse-tree` judges unreadable, the exporter, decoding with yaml.v3 directly, read 7 (more than one document in a file, `!!binary`, `!!set`, a BOM mid-file, the null key `~:`, the non-specific tag `!`, a trailing document holding only `---`) and already refused only 2 (a repeated key, a non-scalar key); these 9 are now a test matrix the three Go readers share. Besides, the Python tools writing conf.d read and wrote it with PyYAML and lost comments, and YAML outside conf.d had PyYAML imitating other readers such as kubectl and Alertmanager.

The root of the problem is "two parsers each read once, and the results must be identical". The only approach that needs no proof of equality is keeping one parser.

## Consequences and known limits

**Impact on users and operators (all breaking changes)**:

- Spellings valid under the old YAML rules may change meaning, and some files become unreadable (§8).
- The exporter does not serve files `parse-tree` judges unreadable: more than one document in a file, tags outside the allow-list such as `!!binary`, a BOM mid-file and the like used to be read by the exporter, and now the alerts of the tenants in them stop. The only mitigations are §8's pre-upgrade guard and the metrics and logs; there is no "read as before" switch.
- Tools writing conf.d now print through yaml.v3, so customers' diffs show formatting changes unrelated to the edit (§2).
- Python tools reading conf.d need da-guard: the da-tools image ships it; the tools look for `$DA_GUARD_BINARY` and then `PATH`; local pre-commit hooks build `.build/da-guard` with Go when `DA_GUARD_BINARY` is unset. When it can be neither found nor built, the tool fails and says why, never falling back to parsing itself.
- tenant-api gains a container; while it or the deployment's policy file is unavailable, every write that changes file content gets a 503 (§5). tenant-api requires git.
- While anything in the tree blocks, writes through tenant-api are refused until it is fixed; batches are all or nothing (§4).
- A CI that runs only da-guard no longer covers routing and policy and needs an extra `da-tools generate-routes --validate --strict` step (§9).

**Non-negotiable acceptance conditions**:

- No reader may be looser than the generator. tenant-api's write criterion is the generator's own result (§4), so for tenant-api this holds by definition.
- "Unreadable" is a closed list: each row of the fixed corpus `tests/shared/parse_tree_corpus.json` carries `parse-tree`'s expected classification, and every `kind` of the list has at least one row.
- For any file, every Go reader reaches the same "unreadable" verdict: they call the same package. The 9 spellings above plus a control run in the exporter (`config_parse_refused_test.go`), da-guard (`cmd/da-guard/parse_verdict_exit_test.go`, exit 3) and `pkg/config/parse_verdict_test.go`; tenant-api answers such a body with 400 (`TestPutTenant_ParseTreeRefusedBodyIs400`).

Tests that hold these behaviors:

- The fault-injection matrix (`tests/ops/test_gsvc_tool_fault_matrix.py`): the (program, subcommand) pairs a real judgement calls are taken from a record, and every pair is hit with an old-version usage error, SIGKILL and other faults, plus each program missing or not executable; the answer can only be a 503 `tool_fault` or a refusal without a verdict, never a pass or findings.
- Deadlines and memory (`tests/ops/test_generator_service.py`): an expired deadline ends the judgement and its descendants, a deadline expiring in the queue, and a memory overrun refusing only that write.
- Each `da-guard write` operation changes only what it edits and writes nothing when refused (`cmd/da-guard/write_test.go`).
- The exit-code order 3 > 1 > 4 (`TestUndetermined_ExitPrecedence`).

**Known limits**:

- **Cost of judging**: every file is judged once more before decoding. The exporter keeps verdicts by the SHA-256 of the file content, up to 8,192 entries, dropping the whole set when full; a process that meets more distinct contents than that keeps dropping it, and the cost falls back to the uncached figure, with the verdicts unaffected.
- **The cache stays**: the generator container's cache, eviction rules and cache-size metrics must be maintained (§3).
- **Image size**: for `da-read prometheus` to read a `prometheus.yml` that uses `kubernetes_sd_configs`, it registers every service discovery, compiling in the cloud SDKs and the Kubernetes client. Measured on 2026-11-24 at commit `d77a3ecb4` with the shipping build (`scripts/ops/go_build.sh`, linux/amd64):

  | `da-read` | Uncompressed / after `gzip -9` |
  |---|---|
  | `da-read` as it is | 81.1 MB / 23.4 MB |
  | The same source without the `discovery/install` import | 20.0 MB / 7.5 MB |

  What it buys is reading `prometheus.yml` exactly as Prometheus does.
- **Getting state needs tenant-api itself**: online, one must be able to `kubectl exec` into the tenant-api pod; offline, to run the deployed tenant-api image with docker to read a backup. A site with neither cannot get it, and `offboard_tenant` can run only with `--skip-state-check`. Only when a site reports this is a separate binary calling the same parse functions built into da-tools.
- **Rebasing yaml.v3**: a later upgrade of the upstream version first compares the old and new base's readings on the fixed corpus.
- **Fetching from git is stricter than the file system**: the same tree can be green in customer CI while tenant-api refuses a write over a symlink (§3).

## Alternatives considered

| Option | Why not |
|---|---|
| Keep each side imitating the other | Each fix exposes another gap; comparison finds gaps but cannot prove equality ("Background") |
| Restrict conf.d to spellings both parsers must read the same way | With values quoted and no advanced features, the two still read differently; such a subset can only be sampled, not defined |
| Parse YAML in Python only, Go reading Python's JSON | The exporter is a runtime component; depending on Python would put a Python runtime in every exporter pod |
| `parse-tree` refuses everything PyYAML refuses | That means rewriting every PyYAML check in Go, Go imitating another parser again; comparing the two kept finding new "only PyYAML refuses" classes. A closed list instead, the rest read as yaml.v3 reads it |
| The generator refuses aliases, merge keys and explicit tags | The reason was narrowing the gap between two parsers; with one parser there is no gap to narrow, yet customers would have to rewrite existing files |
| The exporter only observes and does not refuse unreadable files | That keeps two judgements, and a file could still be served by the exporter and refused by the generator |
| On a write, look only at the findings that belong to it (per-write attribution) | The generator's validation short-circuits: once something fails, later checks do not run; in the counter-examples run, a neighbor's violation hid the written file's problem, and the finding of a broken platform file belonged to no write |
| Compare only which error messages are new after the write | A tenant already in violation rewriting its config produces the very message the base has, and the difference lets it through |
| Do not block when the rendered result before and after is the same | It misses: adding an empty value to `group_by` adds a blocking finding while the rendered result is unchanged |
| Judge in the resident process, without forking a child | One oversized or malicious write could bring down the resident process, turning every later write into a 503 |
| Fall back to Go's judgement when the container is unavailable, or a let-through switch | Whoever can send writes can affect whether the container is available, which would let the requester pick its own judge |
| Readiness looks only at whether the last judgement met a tool fault | Before the first judgement it would always report ready, even with a program missing; a full judgement can be refused early on its content and never reach the faulty program |
| A two-phase switch (old and new side by side, diffs compared) or a switch between them | Two judgements and a divergence catalog to maintain, while Go's judgement was known to be looser, so keeping it bought nothing |
| Finish on an integration branch and merge once | CI is triggered only by pull requests to main and pushes to main, so no step on an integration branch would be guarded by CI |
| Read all YAML with yaml.v3 | The wrong dialect for Kubernetes manifests: kubectl follows YAML 1.1, and `name: no` is `false` in kubectl but `"no"` in yaml.v3 |
| Draw the boundary at `import yaml` | Too wide (a file that only reads GitHub workflows would be blocked), and a wrapper function bypasses it; an AST lint finds the call sites instead |
| Python reads `parse-tree`'s result and derives tenant-api's state records itself | Python would have to rewrite the conversion by field type and every record's refusal rules (`members: abc` is refused whole by tenant-api, yet `parse-tree` reads it), and the two readings would drift apart |
| Commit a tidied copy of the policy into git | Covers policy only, while routing is still parsed by both sides; a forgotten regeneration keeps the old policy in use |
| Port Python's routing and policy semantics to Go in full | Another implementation of the semantics; considered only if the cost of a container were unacceptable |
| The write API accepts only JSON | Solves only tenant-api's writes; customers' hand-written YAML is still parsed in CI and the exporter |

## Implementation locations

| Term in this ADR | Code |
|---|---|
| `parse-tree`'s judgement | `components/threshold-exporter/app/pkg/parsetree`; the da-guard subcommand in `cmd/da-guard/parse_tree.go`, `parse_tree_list.go` |
| Go's file-name rules | `pkg/confdname` |
| Edit primitives and the merge invariant | `pkg/confedit` (`Verify`); `da-guard write` in `cmd/da-guard/write.go` |
| Python reading `parse-tree` | `scripts/tools/_lib_parse_tree.py`; sending writes `scripts/tools/_lib_confedit.py` |
| The generator container | `scripts/tools/ops/generator_service.py`, `_gsvc_child.py`; tenant-api's caller `components/tenant-api/internal/generator` |
| tenant-api's entry to the shared judgement | `Judge` in `internal/confd/verdict.go` |
| tenant-api's `state show` / `config check` | `components/tenant-api/cmd/server/state_cmd.go`, `config_cmd.go`; the Python reader `scripts/tools/_lib_tenant_state.py` |
| `da-crdecode`, `da-read` | `components/threshold-exporter/app/cmd/da-crdecode`, `components/da-read` |
| The vendored yaml.v3 | `components/threshold-exporter/app/third_party/yaml.v3` (described in `README.md` beside it) |
| The deletion-scope declaration table | `tests/shared/phase2_deletion_scope.json` |

## Related

- [ADR-023 tenant-api write plane: the single-writer invariant](./023-write-plane-single-writer-invariant.md)
- [ADR-037 config-diff compares the rendered output](./037-config-diff-compares-rendered-output.en.md)
- [ADR-038 keys no reader reads are judged by measurement](./038-unread-keys-judged-by-readers.en.md)
- Design discussion and measurements: [#2766](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2766) (the semantic layer), [#2486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2486) (the switch and the write criterion), [#2858](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2858) (the syntax layer)
- Industry practice: parse once and leave validation to the authority, as in [Kubernetes server-side dry-run](https://kubernetes.io/blog/2019/01/14/apiserver-dry-run-and-kubectl-diff/); put the parsing tool in the user's pod, as in [Argo CD Config Management Plugins](https://argo-cd.readthedocs.io/en/stable/operator-manual/config-management-plugins/); refuse rather than let through when the validator fails, as in [Kyverno policy settings](https://kyverno.io/docs/writing-policies/policy-settings/).
