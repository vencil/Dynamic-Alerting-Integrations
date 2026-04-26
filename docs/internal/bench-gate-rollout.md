---
title: "Pre-Tag Benchmark Regression Gate — 3-Phase Rollout Plan"
tags: [benchmark, ci, internal, plan]
audience: [maintainers]
verified-at-version: v2.8.0
lang: zh
---

# Pre-Tag Benchmark Regression Gate — 3-Phase Rollout Plan

> **Status**: Phase 1 ✅ landed (PR #65 / issue #60); Phase 2 ⏳ awaiting nightly data accumulation (issue #67, target ~2026-05-23); Phase 3 ⏸️ long-term (depends on Larger Runners adoption).
>
> **Source of truth** for the rollout plan. The original RFC was issue [#60](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/60); the staged-rollout breakdown was previously only in issue [#76](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/76)'s body and now lives here for searchability and to satisfy #76 acceptance #1.

## Background

Phase 1 of the pre-tag bench gate landed in [PR #65](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/65) (issue [#60](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/60)) as a **report-only** Makefile target. This document tracks the staged rollout to Phase 2 (main-only hard gate) and Phase 3 (PR-level gate).

References: planning §12.5 (Spawn task C); archive §S#27 (PR #59 v1→v2 baseline measurements showing ~50% within-run variance).

## Why staged rollout, not immediate hard gate

Gemini's first-pass review of #60 proposed an absolute "do NOT add a hard bench gate" stance. Counterargument from PR #59 evidence:

- **PR #59 v1→v2 baseline** showed *within-run* CV up to ~50% on hot-path benchmarks (e.g. `BenchmarkDiffAndReload_Hierarchical_1000_NoChange` jittered ~190-280 ms across consecutive runs of the same SHA).
- However, *cross-run median-of-5* converged to within 5-10% across the same SHA on consecutive nightly runs (PR #65 was designed against this signal).
- **Conclusion**: single-run absolute thresholds are unreliable; median-of-5 cross-run thresholds are workable — but only after enough nightly samples accumulate to establish what "normal cross-run variance" looks like for *this* repo on the GH free-tier runner.

Hard gate at PR-level is also blocked on infrastructure: PR-level CI currently runs on free-tier GH-hosted runners (~2 vCPU, no isolation), where neighbour noise can spike a single bench run by 2-3×. Larger Runners (paid) would fix this; **Phase 3 entry condition** is "Larger Runners adopted OR equivalent low-noise CI surface".

## Three phases

### Phase 1 — Report-only ✅ landed in PR #65 / issue #60

**Status**: 🟢 implemented + stable.

| Component | Mechanism |
|---|---|
| Local report | `make benchmark-report` writes `.build/bench-baseline.txt` |
| Pre-tag wiring | `make pre-tag` runs `benchmark-report-warn` (informational, no exit code propagation) |
| Nightly sampling | `bench-record.yaml` workflow on `main` only, 90-day retention |
| Cross-run analysis | `analyze_bench_history.py` (PR #71) reports CV + max/min ratio over a sliding window |
| **Release attachment** | `release-attach-bench-baseline.yaml` (PR #117) auto-attaches `bench-baseline-<tag>.txt` to each GitHub Release as an asset, sourced from the latest successful nightly artifact |

### Phase 2 — main-only hard gate at 3× median-of-5 (TBD)

**Status**: ⏳ entry conditions tracked in issue [#67](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/67); review fires ~2026-05-23.

**Entry conditions** (all-of):

- ≥ 28 nightly `bench-record` runs accumulated (4 weeks × 7 days)
- `analyze_bench_history.py` reports `cross_run_cv ≤ 25%` AND `max_min_ratio ≤ 1.30` for ≥ 26 of 28 runs (per existing tool's GO threshold)
- `pre-tag` workflow on a `release/*` branch fails when current run > 3× median of last 5 runs for any tracked benchmark
- **Scope**: only triggered on tag/release branches, not on every PR (PR-level cost is too high — one PR ≈ 30 min × 13 benchmarks × `count=6` = significant CI cost on every PR)

**Acceptance criteria for the Phase 2 implementation PR**:

- Hard gate has been triggered at least once in dry-run mode (artificially injected slowdown) before being enforced
- Bypass mechanism: `[bench-bypass]` tag in commit message, requires maintainer review
- Per cross-review blind spot #5 (rollback condition), Phase 2 workflow lands with `continue-on-error: true` for **2 weeks** before flipping to hard-fail. If false-positive rate during grace ≥ 2 incidents, revert to Phase 1 + revisit.

**Per-benchmark evaluation, not aggregate** (per #60 cross-review blind spot #3):

- Some benchmarks may pass (e.g. pure in-memory `MergePartialConfigs_1000`) while others fail (e.g. I/O-heavy `FullDirLoad_*_1000`).
- Phase 2 schema supports per-bench `threshold_multiplier`. So a partial GO is acceptable: Phase 2 gates the benches that pass, leaves noisier ones in informational mode for now.

### Phase 3 — PR-level hard gate at 3× median-of-5 (long-term)

**Status**: ⏸️ blocked on Larger Runners + Phase 2 stability.

**Entry conditions** (all-of):

- Larger Runners (paid GH-hosted) or self-hosted low-noise runners adopted
- Phase 2 has run for ≥ 8 weeks without false-positive blocks of legitimate work
- Maintainer green-lights the cost trade-off

**Acceptance criteria for the Phase 3 implementation PR**:

- PR-level gate honors PR labels (`bench-skip`) for explicitly non-perf-related PRs
- Gate runs in parallel with regular tests, no critical-path lengthening
- Threshold tightens to 1.5× baseline (vs Phase 2's 3×), justified by reduced runner variance

## Window invalidation conditions

The 4-week window for Phase 2 entry must restart if any of these land on `main`:

- An intentional perf-affecting change (optimization, regression-fix, refactor that touches `scanDirHierarchical` / `diffAndReload` / `fullDirLoad` paths)
- A Go version bump
- Runner image change announced by GitHub (would shift baseline silently)
- Switching to Larger Runners (would invalidate variance baseline; that's Phase 3 anyway)

When an invalidation event lands, comment on issue [#67](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/67) with the date and reset target.

## Pre-Phase-2 tooling

- ✅ `scripts/tools/dx/analyze_bench_history.py` (PR #71) — pulls last N `bench-record` workflow artifacts via `gh api`, parses `bench-baseline.txt`, computes per-benchmark median-of-5 + CV + max-min ratio, outputs Markdown table with GO/NO-GO column.

## When the Phase 2 review fires (issue #67)

1. Run `analyze_bench_history.py` → get the GO/NO-GO table
2. If GO (or partial GO):
   - Comment on #67 with the empirical numbers
   - Open Phase 2 RFC issue (separate from #67) proposing concrete `benchmarks/baseline.json` schema + `.github/workflows/benchmark-gate.yaml` design
   - Maintainer approves → implementation PR
3. If NO-GO:
   - Comment on #67 with the failing benches + measured noise
   - Decide: extend window 2-4 more weeks? Tighten bench scope (drop I/O-heavy)? Move to Larger Runners earlier (Phase 3 first)?

## Cross-references

- Issue [#60](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/60) — original 3-phase rollout RFC (closed via PR #117)
- Issue [#67](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/67) — Phase 2 readiness review stub (open, target ~2026-05-23)
- Issue [#76](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/76) — Phase 2/3 plan codification (this document is the deliverable)
- PR [#59](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/59) / archive §S#27 — 50% within-run variance evidence
- PR [#65](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/65) — Phase 1 implementation
- PR [#71](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/71) — `analyze_bench_history.py`
- PR [#117](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/117) — Phase 1 acceptance #6 (release attachment)
- planning §12.5 — internal tracking row
- [`benchmark-playbook.md`](benchmark-playbook.md) — operational ops content for the bench harness
