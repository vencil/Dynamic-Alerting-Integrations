---
title: "ADR-039: Deliberate performance costs go into an in-repo acceptance ledger that the bench gates check against measurements"
tags: [adr, benchmark, ci, perf-regression]
audience: [platform-engineers, contributors]
version: v2.9.0
lang: en
---

# ADR-039: Deliberate performance costs go into an in-repo acceptance ledger that the bench gates check against measurements

> **Language / 語言：** [中文](./039-bench-acceptance-ledger.md) | **English (Current)**

## Status

✅ **Accepted** (2026-10-31). The ledger and the verdicts of both bench gates (the comparison on PRs and the per-commit attribution after merging to main) are live. A cumulative budget, blocking merges on PRs, and what the ledger does when the reference version changes are covered in §Not yet decided.

Revised 2026-11-01: the absolute-ns/op nightly monitor was retired, so the cumulative budget for the deterministic metrics became the only planned nightly trend detector (see §Not yet decided).

Revised 2026-11-13: "whether bimodal, unstable benchmarks get a class of their own" is now triggered by a bench gate verdict, with no further dedicated measurement.

Revised 2026-11-24: rewritten to describe current behavior; the decision is unchanged.

## Decision

**A deliberate performance cost is written into `.github/bench-accepted.toml` in the same PR that introduces it. Both gates use only the entries that the change being judged added, and check the measurement against the bound each entry declares.**

An entry states four things: which benchmarks (full names, no regular expressions), which metric (`sec/op`, `B/op` or `allocs/op`), the allowed bound (a percentage), and a one-line reason. An optional `issue` field takes the number of the regression issue when the cost is declared after the fact. One entry from the current ledger:

```toml {.quote src=".github/bench-accepted.toml"}
[[accepted]]
benches = ["BenchmarkMergeParsedPlatform_1000_Cached"]
metric = "allocs/op"
max_delta_pct = 8
reason = "寫入檢查的合併也墊內建層（#3832），多 11 個鍵；根層讀不讀得了改用已快取的解碼，不再每次重解"
```

Each row the gate's comparison flags (a statistically significant regression of at least +5%) is judged as follows:

| Entries added by this change | Verdict | What the gate does |
|---|---|---|
| No matching entry | Regression | As before: on the PR, a comment and the `perf-regression` label; an issue after merge |
| Name and metric match, measured ≤ bound | Accepted | Not a regression. No label, no issue; listed in the run summary only |
| Name and metric match, measured > bound | Regression | The issue and the comment show both the declared bound and the measurement |

"Match" needs both the name and the metric: an entry declaring `allocs/op` does not cover a `B/op` regression of the same benchmark. When a row matches several entries, the widest bound among them applies.

### Example

Input: the entry above is added by this change; the benchstat comparison flags two rows. The first row has only `sec/op` flagged, so the gate runs a second, independent interleaved pass, which does not flag it again.

```text
IncrementalLoad_1000_NoChange-4      6.912m ± 1%    7.403m ± 1%   +7.10% (p=0.002 n=6)     ← sec/op
MergeParsedPlatform_1000_Cached-4    8.000k ± 0%    8.520k ± 0%   +6.50% (p=0.002 n=6)     ← allocs/op
```

Output (the values `scripts/tools/dx/bench_verdict.py verdict` writes for the workflow, omitting the first five lines: `inconclusive`, `inconclusive_reason`, `benchstat_txt`):

```text
regression_basis=timing-not-replicated
regression=false
unreplicated<<__UNREPEOF__
IncrementalLoad_1000_NoChange-4      6.912m ± 1%    7.403m ± 1%   +7.10% (p=0.002 n=6)
__UNREPEOF__
accepted<<__ACCEOF__
MergeParsedPlatform_1000_Cached-4    8.000k ± 0%    8.520k ± 0%   +6.50% (p=0.002 n=6)  [declared ≤ +8%: 寫入檢查的合併也墊內建層（#3832），多 11 個鍵；根層讀不讀得了改用已快取的解碼，不再每次重解]
__ACCEOF__
```

With the same entry, a measured `allocs/op` of +9.20% exceeds the bound. A deterministic regression then remains, so the gate runs no second pass, and the `sec/op` row counts as a regression too (the same five lines omitted):

```text
regression_basis=deterministic
regression=true
regressions<<__EOF__
IncrementalLoad_1000_NoChange-4      6.912m ± 1%    7.403m ± 1%   +7.10% (p=0.002 n=6)
MergeParsedPlatform_1000_Cached-4    8.000k ± 0%    8.736k ± 0%   +9.20% (p=0.002 n=6)
__EOF__
over_bound<<__OVEREOF__
MergeParsedPlatform_1000_Cached-4    8.000k ± 0%    8.736k ± 0%   +9.20% (p=0.002 n=6)  [declared ≤ +8% in .github/bench-accepted.toml]
__OVEREOF__
```

### Which entries count as "added by this change"

The ledger at each end of the compared range is parsed into a set of entries; the entries present at the head and absent at the base are the added ones. Parsed entries are compared, not text lines. An entry's identity is three things: the set of benchmarks, the metric and the bound. Reformatting, reordering fields or the names inside `benches`, rewording the reason, or adding an `issue` leaves it the same entry and does not bring an old entry back into effect; widening the bound, changing the metric, or adding or removing a benchmark is a new acceptance, reviewed in that PR.

| Gate | Base | Head |
|---|---|---|
| On the PR | merge-base | PR head |
| After merge, a single-commit push | the parent commit | this commit |
| After merge, a push of several commits | main before the push | main after the push |

Which commit an entry belongs to is decided by git (the commit that added it); the file holds no SHAs. This survives squashing and needs no bot writing back to main. Reverting the PR that introduced a cost removes its entry together with the code.

### Guarding the ledger itself

- If the ledger is not valid TOML, has an unknown field, misses a required field or has a wrong type, the verdict step fails (exit code 2). A broken ledger is neither read as "no entries" nor as "everything accepted".
- Every benchmark name in an entry must exist in the current threshold-exporter code (which is what the bench gates measure) and be defined in exactly one place. When a benchmark is renamed or removed, a leftover entry turns CI's Python tests red instead of silently matching nothing; a name defined in two places turns them red as well, because benchstat's output rows carry no package.
- CODEOWNERS names an owner for the ledger file, so a PR that changes it requests the owner's review automatically. Branch protection does not require code-owner approval, so this is a request, not a mechanical block.
- A regression from a base of zero (for example, a path that did not allocate and now does) has no percentage in benchstat; the verdict treats it as an unbounded increase that no bound covers.

## Context

There are two bench gates, and they share one verdict:

- **On the PR**: PR head against the merge-base. A regression gets a comment and the `perf-regression` label; merging is not blocked.
- **After merging**: each commit on main against its parent (with squash merges, one commit is one PR). A regression opens an issue, which closes automatically only when a re-run of the same commit is no longer judged a regression.

Some regressions are deliberate: an extra decode for correctness, one more field in a struct. Without a ledger, the author can only write the trade-off in a PR comment and merge. The post-merge gate cannot read PR comments, so it measures again and opens an issue; a real trade-off does not go away on a re-run, so the issue stays open, and each one needs someone to look at it and close it by hand.

[ADR-032](032-paired-interleaved-bench-measurement.md) §7 sees the same thing from the nightly monitor's side: the monitor needs a third exit, "attributed and accepted", and leaves two sub-questions open, where the mark lives and how fine-grained it is. This ADR's answer: in a ledger in the repo, at the grain of "benchmark × metric × bound".

## Consequences and known limitations

- **Authors have to estimate a bound.** `B/op` and `allocs/op` are identical run to run for the same input, so the number the PR bench gate measured plus a small margin is enough; `sec/op` is noisy and needs a wider bound.
- **The bound is the loose side.** When the measurement is well below the bound, the remaining margin has no effect on this verdict, and it does not carry over to later commits either: each gate only looks at the entries added by the change it is judging.
- **Accepting after the fact means closing the issue by hand.** If the decision to accept comes after merging, add the entry in a later PR (the `issue` field is available), then after it merges comment on the issue with a link to the entry and close it. This step is not automated.
- **For a push of several commits, attribution still measures only the last commit.** The ledger takes in entries added anywhere in the push, but regressions in the commits in between are not measured.
- **Sub-benchmarks (`BenchmarkX/case`, created by `b.Run`) cannot be declared.** Names must have the form `BenchmarkX`; none of the current threshold-exporter benchmarks has sub-benchmarks, and the naming rule must be extended when one appears.
- **The merge-base and the parent commit can differ.** The Δ measured on the PR and the Δ measured after merging are not always the same; the post-merge gate is the one that opens issues, so the bound should cover both.
- **Drift that stays below the per-commit threshold has no nightly automatic detection today.** The nightly paired ns/op only writes a run summary; it files no issues and does not read the ledger. The release-time comparison (`bench-gate-release.yaml`, against the previous platform release, α=0.05, |Δ| ≥ 10%) reports such regressions without blocking the release. The cumulative budget that takes over is in the next section.

## Not yet decided

| Item | When it gets decided |
|---|---|
| **A cumulative budget for the deterministic metrics**: add `B/op` and `allocs/op` to the nightly main-versus-reference ratio, to catch drift that stays below 5% per commit but adds up. This is the only planned nightly trend detector: the absolute-ns/op monitor was retired and paired ns/op is informational | The nightly run already records them: on nights where the paired measurement succeeds, the output (`bench-paired/v3`) has a `deterministic` field with the `B/op` and `allocs/op` medians and ratio for each benchmark that has data on both sides, recorded and not judged; a night recorded as inconclusive has no such field (`scripts/tools/dx/pair_bench_ratio.py`). After at least four weeks of it, measure their real night-to-night noise, then choose a budget. The design must first solve a known problem: if the expected value is the product of the bounds, every accepted bound becomes headroom that later regressions can spend, and improvements are counted as headroom too |
| **Blocking merges on PRs for undeclared deterministic regressions** | The owner decides once the ledger has been live for four weeks and every deterministic regression flagged in that period could be resolved with the ledger |
| **What the ledger does when the reference version changes** (ADR-032, section 「待決與已決的設計題」, question 1) | Decided together with the cumulative budget. The nightly run currently files no issues and does not read the ledger, so changing the reference closes nothing; if the cumulative budget files issues, ADR-032's invariant (changing the reference must not close an open performance issue) must hold in its design |
| **Whether bimodal, unstable benchmarks get a class of their own** | This means `BenchmarkIncrementalLoad_1000_OneFileChanged`: measured repeatedly on GitHub runners, its sec/op ratio splits into a high and a low group within a single day, about 26 percentage points apart (+30.38% vs +3.98%; measurements in [#1497](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1497)). Decided when either bench gate flags it with `timing-replicated`: only sec/op was flagged, and a second independent interleaved pass flagged the same benchmark (the PR gate's verdict summary says `sec/op only, confirmed`; the post-merge attribution issue says `sec/op only, confirmed by a second independent interleaved pass`). That verdict by itself means `B/op` and `allocs/op` were not flagged. The numbers from those two passes then decide: a class of its own, or its sec/op goes into the acceptance ledger. The replication pass is not guaranteed to stop this spread: when both passes happen to land on the same side, the gate can still flag a regression that does not exist. No further dedicated measurement is done for it |

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

- [ADR-032](032-paired-interleaved-bench-measurement.md): paired interleaved nightly measurement; this ADR answers "where the mark lives" and "how fine-grained" from its §7
- [#3468](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3468): lifecycle of performance-regression issues
