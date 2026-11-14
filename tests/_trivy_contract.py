"""The one Trivy scan filter every CI scan of our images uses (#3870).

Three workflows scan images with `aquasecurity/trivy-action`: release.yaml
(the pre-publish hard gate, `exit-code: 1`), nightly-image-scan.yaml (capture,
`exit-code: 0`) and component-docker-build.yaml (PR-time, informational). They
differ in whether a finding fails the run; they must NOT differ in what counts
as a finding, or the nightly early warning and the PR-time output stop
predicting what will block a release.

So the filter lives here, once, and each consumer's guard imports it:
  - tests/ops/test_release_scan_before_publish.py
  - tests/ops/test_nightly_scan_matrix_drift.py
  - tests/shared/test_trivy_scan_contract.py (asserts every trivy-action step
    in every workflow carries it).

⛔ `ignore-unfixed: true` is a stance, not a default: the release gate blocks
on what a rebuild or a bump can fix. A finding with no fixed version is the
nightly scan's job (it reports with the same filter, so an unfixed finding
never blocks a release and is never silently dropped either — it surfaces
there once a fix ships). Turning it off would make every upstream-unfixed CVE
stop every release with no lever to clear it. Change it here or not at all:
the three consumers move together.

Not in this module, on purpose: `exit-code` (it is the one thing the three
consumers legitimately differ on) and `trivyignores` (scoped per image; see
each guard).
"""
from __future__ import annotations

#: What counts as a finding. `severity` filters AT SCAN TIME — a HIGH dropped
#: here never reaches any downstream report — so it is pinned exactly, not
#: "contains CRITICAL".
SCAN_FILTER: dict[str, object] = {
    "severity": "CRITICAL,HIGH",
    "ignore-unfixed": True,
}

#: trivy-action inputs that say WHAT to scan and WHERE the answer goes. They
#: cannot shrink what Trivy looks for. `version` picks the binary and is pinned
#: by tests/shared/test_toolchain_pin_parity.py. One value of `format` is NOT
#: neutral — see SARIF_FORMAT.
NEUTRAL_KEYS = frozenset({"image-ref", "format", "output", "version"})

#: trivy-action v0.36.0 entrypoint.sh: `format: sarif` without
#: `limit-severities-for-sarif: true` runs `unset TRIVY_SEVERITY`, so the
#: pinned severity silently stops applying to that consumer.
SARIF_FORMAT = "sarif"


def filter_mismatches(with_: dict) -> list[str]:
    """Each way a trivy-action `with:` stops applying SCAN_FILTER."""
    problems = [
        f"{key}={with_.get(key)!r} (expected {want!r})"
        for key, want in SCAN_FILTER.items()
        # str(): YAML `true` loads as True; a quoted "true" is a different
        # literal and is refused rather than guessed at.
        if key not in with_ or str(with_[key]) != str(want)
    ]
    if str(with_.get("format", "")).strip().lower() == SARIF_FORMAT:
        problems.append(
            f"format={with_['format']!r} — trivy-action unsets the severity filter for sarif"
        )
    return problems
