## Summary

<!-- Brief description of the changes -->

## Type of Change

- [ ] Feature (new functionality)
- [ ] Bug fix
- [ ] Documentation
- [ ] Refactoring (no behavior change)
- [ ] CI/CD / Tooling
- [ ] Release

## Documentation Checklist

<!-- For documentation-related PRs, check applicable items -->

- [ ] Frontmatter `version` updated (`check_frontmatter_versions.py --fix`)
- [ ] Cross-language counterpart updated (ZH ↔ EN)
- [ ] No orphan documents introduced (new .md files linked from at least one other doc)
- [ ] CHANGELOG.md updated (if user-facing change)
- [ ] Numbers accurate (tool count, scenario count, Rule Pack count match source of truth)
- [ ] No codenames or jargon — descriptive names only

## Quality Gates

```bash
make version-check                                    # Version consistency
make lint-docs                                        # Documentation lint
pre-commit run --all-files                            # Auto hooks
pre-commit run --hook-stage manual --all-files        # Manual hooks (heavier)
```

## Test Plan

<!-- How was this tested? -->

## Pre-merge Self-Review

> **Discipline**: pre-merge deep self-review is **default for every PR**, not opt-in. If user has to prompt "做 self-review 了嗎?" before merge, you skipped this section. See [`testing-playbook.md` §v2.8.0 LL §5+§6](../docs/internal/testing-playbook.md#v2-8-0-lessons-learned-2026-04-23-phase-a) for rationale.

### Pass 1 — 5+1 standard checks (S#73)

<!-- Strikethrough N/A items rather than ticking them. -->

- [ ] (1) Function/API signatures match every caller; no orphan signature changes
- [ ] (2) Module-level constants for stable identity where `useMemo`/`useCallback` deps care
- [ ] (3) Rules-of-Hooks discipline (hooks invoked unconditionally above early returns)
- [ ] (4) Wiring triple complete (front-matter `dependencies` + `import` block + `window.__X` self-register)
- [ ] (5) Conditional **usage** (not conditional invocation) for hooks
- [ ] (6) **Verify-reference**: APIs / library behaviors / hook scripts read & empirically confirmed (not assumed). Includes hooks I didn't write — PR #164 found `_batch_cat_blobs` Popen pipe deadlock latent for months because nobody verified the hook script.

### Pass 2 — deeper scrutiny (S#77 / PR #166 amend)

- [ ] **Counts / numbers in PR body / CHANGELOG / docstrings cross-checked** against `pytest --collect-only`, file `wc -l`, raw audit. (LL §5 case (i): "Path derivation × 5" vs actual 10 — exact instance happened in PR #171 v1.)
- [ ] **Internal helper functions have direct tests**, not only via integration / parametrized fixtures. If you wrote an `if`-branch but no test walks it, it's hidden dead code.
- [ ] **Edge cases tested**: paths-outside-`PROJECT_ROOT` / single-element / empty / boundary values. PR #166 amend caught `BlobViolation.render()` crash on `tmp_path` exactly because new `TestMain` fixtures landed.
- [ ] `tmp_path` (or equivalent) fixtures stress assumptions about where input comes from.

### Pass 2 — regression tests specifically (LL §6)

- [ ] **Intentional-break dogfood loop** done: backup fix → revert to broken pattern → run test → confirm fail → restore → re-run pass. **Without this loop, the regression test is article-of-faith** (see PR #166's 300×1KB headline test that didn't actually trigger Windows pipe-buffer deadlock; only 1000×2KB did).
- [ ] Defense-in-depth ladder: pytest-timeout marker (Layer 1 fast-fail) + soft `assert elapsed < X.0` budget (Layer 2 slow-regression catch) + inner production timeout (Layer 3 actual fix).
- [ ] Empirical threshold table in docstring if test parameters are tuned (path count vs total bytes vs OS pipe buffer).

### Anti-patterns to flag yourself for

- [ ] I'm NOT skipping pass 2 because "this PR is small / doc-only / refactor"
- [ ] I'm NOT confusing `pass` with `pytest.skip("TODO")` in stub methods (silent green vs xfail-style visible — see PR #171 amend Fix 2)
- [ ] I'm NOT reactively expanding the PR scope to fix things I noticed during self-review without bumping the PR description / commits accordingly

### Optional: trailer in last commit

For PRs where you ran intentional-break dogfood, include the result in commit message:

```
Self-Review-Pass-2: dogfood mutated <function>; <test_name> caught (✓)
```

---

<!-- Don't tick boxes you didn't actually do. The honesty contract is the point. -->

