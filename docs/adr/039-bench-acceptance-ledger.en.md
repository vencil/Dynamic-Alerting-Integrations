---
title: "ADR-039: Deliberate performance costs go into an in-repo acceptance ledger that the bench gates check against measurements"
tags: [adr, benchmark, ci, perf-regression]
audience: [platform-engineers, contributors]
version: v2.9.0
lang: en
id: ADR-039
tracking_kind: adr
status: accepted
domain: dx
created_at: 2026-10-31
updated_at: 2026-10-31
---

# ADR-039: Deliberate performance costs go into an in-repo acceptance ledger that the bench gates check against measurements

## Status

✅ **Accepted** (2026-10-31). The ledger and the verdicts of both gates (the bench gate on PRs and the per-commit attribution after merging to main) are live. A cumulative budget, blocking merges on PRs, and what the ledger does when the reference version is changed are covered in §Not yet decided.

## Context

There are two bench gates, and they share one verdict:

- **On the PR**: PR head against the merge-base. A regression gets a comment and the `perf-regression` label; merging is not blocked.
- **After merging**: each commit on main against its parent (with squash merges, one commit is one PR). A regression opens an issue, which closes automatically only when a re-run of the same commit comes back clean.

Some regressions are deliberate: an extra decode for correctness, one more field in a struct. Until now the author wrote the trade-off in a PR comment and merged. The post-merge gate cannot read PR comments, so it measured again and opened an issue; a real trade-off never comes back clean on a re-run, so the issue stayed open. An inventory on 2026-10-31 found 8 such issues open at once, the oldest for five weeks; in 3 of them the trade-off had been written on the PR before it merged.

[ADR-032](032-paired-interleaved-bench-measurement.md) §Open question 7 saw the same thing from the nightly monitor's side: the monitor needs a third exit, "attributed and accepted", with open sub-questions including where the mark lives and how fine-grained it is.

## Decision

**A deliberate performance cost is written into `.github/bench-accepted.toml` in the same PR that introduces it. Both gates use only the entries that the change being judged added, and check the measurement against the bound each entry declares.**

An entry states four things: which benchmarks (full names, no regular expressions), which metric (`sec/op`, `B/op` or `allocs/op`), the allowed bound (a percentage), and a one-line reason.

```toml
[[accepted]]
benches = ["BenchmarkScan_1000"]
metric = "allocs/op"
max_delta_pct = 35
reason = "每份檔多一次泛型解碼，判準才與 merged_hash 一致"
```

Each flagged row (a significant regression of at least +5%) is judged as follows:

| Entries added by this change | Verdict | What the gate does |
|---|---|---|
| No matching entry | Regression | As before: PR comment and label; an issue after merge |
| Name and metric match, measured ≤ bound | Accepted | Not a regression. No label, no issue; listed in the run summary only |
| Name and metric match, measured > bound | Regression | The issue and the comment show both the declared bound and the measurement |

"Match" needs both the name and the metric: an entry declaring `allocs/op` does not cover a `B/op` regression of the same benchmark.

### Example

Input: the entry above is added by this change; the benchstat comparison flags these two rows (a second interleaved pass did not flag `Merge`'s `sec/op` again).

```text
Merge_1000-4   949.8µ ± 1%   1017.2µ ± 1%  +7.10% (p=0.002 n=6)     ← sec/op
Scan_1000-4    7.000k ± 0%   9.100k ± 0%  +30.00% (p=0.002 n=6)     ← allocs/op
```

Output (the values `scripts/tools/dx/bench_verdict.py verdict` writes for the workflow):

```text
regression_basis=timing-not-replicated
regression=false
unreplicated<<…
Merge_1000-4   949.8µ ± 1%   1017.2µ ± 1%  +7.10% (p=0.002 n=6)
accepted<<…
Scan_1000-4    7.000k ± 0%   9.100k ± 0%  +30.00% (p=0.002 n=6)  [declared ≤ +35%: 每份檔多一次泛型解碼，判準才與 merged_hash 一致]
```

With the bound written as 20, the `Scan` row exceeds it and the verdict goes back to regression:

```text
regression_basis=deterministic
regression=true
over_bound<<…
Scan_1000-4    7.000k ± 0%   9.100k ± 0%  +30.00% (p=0.002 n=6)  [declared ≤ +20% in .github/bench-accepted.toml]
```

### Which entries count as "added by this change"

The ledger at each end of the compared range is parsed into a set of entries; the entries present at the head and absent at the base are the added ones. Parsed entries are compared, not text lines, so reformatting or reordering fields does not bring an old entry back into effect.

| Gate | Base | Head |
|---|---|---|
| On the PR | merge-base | PR head |
| After merge, a single-commit push | the parent commit | this commit |
| After merge, a push of several commits | main before the push | main after the push |

Which commit an entry belongs to is decided by git (the commit that added it); the file holds no SHAs. This survives squashing and needs no bot writing back to main. Reverting the PR that introduced a cost removes its entry together with the code.

### Guarding the ledger itself

- If the ledger fails to parse, misses a field or has a wrong type, the verdict step fails (exit code 2). A broken ledger is neither read as "no entries" nor as "everything accepted".
- Every benchmark name in an entry must exist in the current code. When a benchmark is renamed or removed, a leftover entry turns CI's Python tests red, instead of silently matching nothing or later matching a new benchmark that reuses the name.
- The ledger file is protected by CODEOWNERS; adding an entry needs owner review.
- A regression from a base of zero (for example, a path that did not allocate and now does) has no percentage in benchstat; the verdict treats it as an unbounded increase that no bound covers.

## Consequences and known limitations

- **Authors have to estimate a bound.** `B/op` and `allocs/op` are identical run to run for the same input, so the number the PR bench gate measured plus a small margin is enough; `sec/op` is noisy and needs a wider bound.
- **The bound is the loose side.** When the measurement is well below the bound, the remaining margin has no effect on this verdict, and it does not carry over to later commits either: each gate only looks at the entries added by the change it is judging.
- **Accepting after the fact means closing the issue by hand.** If the decision to accept comes after merging, add the entry in a later PR (the `issue` field is available), then after it merges comment on the issue with a link to the entry and close it. This step is not automated.
- **For a push of several commits, attribution still measures only the last commit.** The ledger takes in entries added anywhere in the push, but regressions in the commits in between were never measured.
- **The merge-base and the parent commit can differ.** The Δ measured on the PR and the Δ measured after merging are not always the same; the post-merge measurement decides, so the bound should cover both.

## Not yet decided

| Item | When it gets decided |
|---|---|
| **A cumulative budget for the deterministic metrics**: add `B/op` and `allocs/op` to the nightly main-versus-reference ratio, to catch drift that stays below 5% per commit but adds up | First have the nightly run parse and record `B/op` and `allocs/op` for at least four weeks to measure their real night-to-night noise, then choose a budget. The design must first solve a known problem: if the expected value is the product of the bounds, every accepted bound becomes headroom that later regressions can spend, and improvements are counted as headroom too |
| **Blocking merges on PRs for undeclared deterministic regressions** | The owner decides once the ledger has been live for four weeks and every deterministic regression flagged in that period could be resolved with the ledger |
| **What the ledger does when the reference version changes** (ADR-032 §Open question 1) | Decided together with the cumulative budget. Until then the nightly monitor does not read the ledger, and ADR-032's invariant (changing the reference must not close an open performance issue) is unaffected |
| **Whether bimodal, unstable benchmarks get a class of their own** | Per ADR-032 §Open question 7, once that benchmark's measurement has a conclusion |

## Alternatives considered

| Alternative | Why not |
|---|---|
| A label on the issue | Not version-controlled, cannot carry a bound, does not disappear on revert |
| A commit-message trailer (e.g. `Perf-Accepted:`) | The squash message is written at merge time and is not visible in review; several entries are awkward to write; whoever merges can change it |
| A "one-click acknowledge" in an external service | The record is not in the repo, so it cannot be reviewed or reverted with the code |
| Resetting the baseline to the new value on acceptance | That is silent absorption: a further regression on top of the new baseline looks like nothing happened |
| Benchmark names without a bound | A further regression on top of the accepted baseline would be covered entirely |
| Regular expressions for benchmarks | One entry can cover benchmarks nobody meant to accept, and a rename goes unnoticed |

## Implementation

- Ledger: `.github/bench-accepted.toml` (field reference in its header)
- Verdict: `scripts/tools/dx/bench_verdict.py` (`parse_ledger`, `added_entries`, `apply_ledger`)
- Orchestration: `scripts/tools/ops/bench_gate_compare.sh` (`LEDGER_BASE_SHA` / `LEDGER_HEAD_SHA`)
- Base for a multi-commit push after merge: `.github/workflows/bench-attrib-main.yaml`
- Tests and the ledger lint: `tests/dx/test_bench_verdict.py`

## Related

- [ADR-032](032-paired-interleaved-bench-measurement.md): paired interleaved nightly measurement; this ADR answers "where the mark lives" and "how fine-grained" from its §Open question 7
- [#3468](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3468): lifecycle of performance-regression issues
