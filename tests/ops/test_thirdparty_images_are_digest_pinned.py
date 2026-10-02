"""Every deployed THIRD-PARTY image ref carries an `@sha256:` digest (#2594 H).

Before this module the digest pin was held by RECONCILIATION, not by a RULE
(measured in #2563, written into platform-log-aggregation-runbook.md §7.6 T5):

* drop the digest from an EXISTING pin and a set comparison goes red —
  `test_nightly_scan_matrix_drift.py` (deploy refs != scan matrix) or
  `test_renovate_config.py` (`EXPECTED_DEPNAMES`);
* add a NEW tag-only third-party image, together with its scan-matrix row and
  the report's EXPECTED count, and nothing goes red: both sides of every
  comparison move together, and Renovate's image managers all key on `@sha256:`, so a
  tag-only ref is simply invisible to `test_renovate_config.py`.

This is the rule. Its subject set is exactly what
`scripts/ops/check_image_refs_resolve.py` reports for the default (`deploy`)
scope — `discover_refs()`, the same function `--list` prints and the nightly
matrix is pinned to. ⛔ Reused, not re-derived: the source globs
(`SOURCE_GLOBS`) and the first-party / locally-built exclusions
(`SKIP_REPO_PREFIXES`, `LOCAL_BUILT_IMAGES`) come from the extractor, so there
is no second list here to drift from it.

⛔ `discover_refs()` returns only refs with a usable tag, so a ref with NO tag
(`image: nginx`, i.e. `:latest`; or a values block with `tag: ""`) never
reached the rule above and added nothing red (#2605 R, the post-hoc review of
#2600). `discover_dropped_refs()` reports exactly those — same sources, same
third-party filter — and `test_no_thirdparty_ref_is_dropped_for_lack_of_a_tag`
asserts it is empty.

Relation to `test_renovate_config.py`'s `EXPECTED_DEPNAMES`: not asserted here,
because it is already implied. Once every extractor ref carries a digest, the
scan matrix (pinned to the extractor by set equality) does too, so Renovate's
matrix manager extracts the new row and `test_scan_matrix_and_deploy_refs_share_depnames`
demands the depName in `EXPECTED_DEPNAMES` plus a `# renovate:` annotation on
the deploy side. Measured for #2594: the same new image WITH a well-formed
digest, values + template + matrix + EXPECTED count updated but
`EXPECTED_DEPNAMES` untouched, turns two tests in that module red.

⛔ SCOPE — deliberately NOT covered, do not read a green run as more:

* **First-party images** (`ghcr.io/vencil/`) are not digest-pinned on purpose;
  their currency is the release pipeline's (#902 out of scope).
* **The `delivered` scope** (`--scope delivered`, what `da-tools init` writes
  into a customer repo) is tag-only on purpose — see the extractor's notes next
  to `_SCOPES`.
* **try-local/** is tag-only on purpose: nothing bumps it, so a digest would
  freeze the demo on a stale layer (`test_trylocal_compose_pins.py` docstring).
* **Refs the extractor cannot see.** A ref hardcoded in a Helm TEMPLATE, or an
  overlay that overrides only `tag:`, is not in `discover_refs()`; those are
  pushed INTO this set indirectly — `test_nightly_scan_matrix_drift.py` demands
  them in the scan matrix, and the matrix must equal this set.
* **Grafana plugins** are not images. Their version pin is a separate rule:
  `test_grafana_plugins_are_version_pinned.py` (TRK-2605, runbook §7.6 T5).
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXTRACTOR = REPO / "scripts" / "ops" / "check_image_refs_resolve.py"

_spec = importlib.util.spec_from_file_location("_cir_digest_rule", EXTRACTOR)
_cir = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cir)

# A digest-pinned ref ENDS in a full sha256. Anchored at the end so that a
# digest-looking substring inside a tag cannot satisfy it.
_PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")

# ⛔ Vacuity floor = the count on main when this rule landed (#2594). It is a
# FLOOR, not a pin: dropping a third-party image legitimately means lowering it.
# One total is enough to catch a lost source — measured, the helm globs alone
# yield 9 of these refs and the k8s globs alone 7, so losing either glob drops
# the total well below 15. A per-source floor would have to re-walk
# `SOURCE_GLOBS` here, i.e. a second copy of the extractor's file selection.
_MIN_REFS = 15


def _thirdparty_refs() -> set[str]:
    return _cir.discover_refs(REPO)


def test_every_thirdparty_image_ref_is_digest_pinned() -> None:
    refs = _thirdparty_refs()
    unpinned = sorted(r for r in refs if not _PINNED.search(r))
    assert not unpinned, (
        "third-party image ref(s) without an `@sha256:` digest:\n"
        + "\n".join(f"  - {r}" for r in unpinned)
        + "\nPin them (`repository` + `tag` + `digest: \"sha256:…\"` in values, or "
          "`repo:tag@sha256:…` in a manifest), add a `# renovate:` annotation and "
          "the depName to EXPECTED_DEPNAMES in tests/ops/test_renovate_config.py. "
          "If the image is actually first-party, it belongs under "
          f"{_cir.SKIP_REPO_PREFIXES} instead."
    )


def test_no_thirdparty_ref_is_dropped_for_lack_of_a_tag() -> None:
    dropped = sorted(_cir.discover_dropped_refs(REPO))
    assert not dropped, (
        "third-party image ref(s) with no usable tag — `discover_refs()` skips "
        "them, so the digest rule above never sees them:\n"
        + "\n".join(f"  - {r}" for r in dropped)
        + "\nPin them like any other third-party image (`tag` + `digest`, or "
          "`repo:tag@sha256:…`). A YAML `tag: 11` is an int, not a string: quote it."
    )


def test_the_dropped_collector_reports_what_it_must() -> None:
    """Control for the test above: what the extractor drops must reach the
    collector, and only third-party, non-template refs may count."""
    def dropped_of(doc) -> set[str]:
        out: set[str] = set()
        _cir._refs_from_node(doc, out)
        return out

    assert dropped_of({"image": "nginx"}) == {"nginx"}
    assert dropped_of({"image": {"repository": "nginx", "tag": ""}}) == {"nginx (tag: '')"}
    assert dropped_of({"image": {"repository": "mariadb", "tag": 11}}) == {"mariadb (tag: 11)"}
    assert dropped_of({"image": {"repository": "nginx", "digest": ""}}) == {"nginx (tag: None)"}
    for clean in ({"image": "nginx:1.27.0"},
                  {"image": "{{ .Values.image }}"},
                  {"image": {"repository": "nginx", "tag": "1.27.0"}},
                  {"source": {"repository": "https://example.com/r.git"}}):
        assert dropped_of(clean) == set(), clean
    # the public function applies the same first-party / local-build filter
    assert _cir._keep("nginx") and not _cir._keep("ghcr.io/vencil/da-portal")


def test_the_rule_has_subjects() -> None:
    refs = _thirdparty_refs()
    assert len(refs) >= _MIN_REFS, (
        f"the extractor reports only {len(refs)} third-party ref(s) (floor "
        f"{_MIN_REFS}) — the digest rule above would pass by checking almost "
        f"nothing. Did SOURCE_GLOBS or the skip lists in {EXTRACTOR.name} change?"
    )


def test_the_predicate_rejects_what_it_must() -> None:
    """Control: the rule is only as strong as `_PINNED`. If this regex ever
    accepts a tag-only or truncated ref, the main test goes vacuously green."""
    full = "sha256:" + "0123456789abcdef" * 4
    assert _PINNED.search(f"nginx:1.27.0@{full}")
    assert _PINNED.search(f"quay.io/o/p@{full}")
    for bad in ("nginx:1.27.0",
                "nginx:1.27.0@sha256:deadbeef",
                f"nginx:1.27.0@{full.upper()}",
                f"nginx:1.27.0@{full}-extra",
                f"nginx:sha256-{full[7:]}"):
        assert not _PINNED.search(bad), bad
