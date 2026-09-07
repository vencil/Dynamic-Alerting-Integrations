#!/usr/bin/env python3
"""One RFC 1123 accept/reject matrix for the three tenant-name gates (#1713).

`operator_generate.validate_tenant_name`, `migrate_to_operator.validate_tenant_name`
and `init_project._validate_tenant_name` are the same K8s object-name gate
written three times — the first two share one regex verbatim, the third
spells the 63-character limit as `len(name) <= 63` instead of `{0,61}`.
Before this file only two of the three had a direct test, and the copy that
`operator_generate.discover_tenant_configs` actually uses had none:
`test_operator_generate_v2.py::test_carriers_the_exporter_would_not_serve_are_not_tenants`
reaches it with a dot-prefixed stem only.

Measured on #1713's base: with that copy's body replaced by
`return not name.startswith(".")`, `discover_tenant_configs` on a conf.d of
`ok-a / DB-A / db_a / db-a- / .hidden` returned
`['DB-A', 'db-a-', 'db_a', 'ok-a']` with 1 warning line instead of `['ok-a']`
with 4, and `test_operator_generate_v2.py` stayed at 0 failed.

Why one matrix over three copies rather than three files: the copies are
supposed to be interchangeable, so a probe is parametrised over all three and
a copy that drifts fails BY NAME (`operator_generate-DB-A`), not as a
disagreement someone has to bisect. `test_the_three_copies_agree_on_every_probe`
is the same fact stated once more as an equality so a probe added to only one
face is still checked across copies.

⛔ `alert_quality._TENANT_NAME_RE` (`^[a-zA-Z0-9_-]+$`) shares the identifier
and NOT the purpose: it is the PromQL/Alertmanager label-value injection
whitelist for `--tenant`, not a K8s object-name gate — it takes `DB_A`, which
every gate here rejects. It is deliberately left out of this matrix; whether
it should be tightened is a question for that tool's owner, not a test.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_OPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _OPS_DIR)
sys.path.insert(0, os.path.join(_OPS_DIR, '..'))

import init_project  # noqa: E402
import migrate_to_operator  # noqa: E402
import operator_generate  # noqa: E402


IMPLEMENTATIONS = [
    ("operator_generate", operator_generate.validate_tenant_name),
    ("migrate_to_operator", migrate_to_operator.validate_tenant_name),
    ("init_project", init_project._validate_tenant_name),
]

# 63 characters is the RFC 1123 label ceiling; the last ACCEPT row sits on it
# and the last REJECT row is one past it.
_ON_CEILING = "a" + "-" * 61 + "a"       # 63 characters
_ONE_PAST_CEILING = "a" + "-" * 62 + "a"  # 64 characters
ACCEPT = ["ok-a", "a", "0", "db1", "svc-01", "a-b-c", _ON_CEILING]
# `a.b` separates the DNS-1123 LABEL these gates enforce from the DNS-1123
# SUBDOMAIN a K8s `metadata.name` would also take: a copy rewritten to the
# subdomain regex passes every other row (`db..a` is an empty label under
# both) and fails only here.
# `ok-a\n` is the #1779 row: `re.match(r"^...$")` lets `$` succeed before a
# trailing newline, so a conf.d stem literally named `evil\n` reached
# `metadata.name: da-tenant-evil\n`; only `re.fullmatch` refuses it.
REJECT = ["DB-A", "db_a", "db-a-", "-lead", "db..a", "a.b", "a b", "",
          _ONE_PAST_CEILING, "ok-a\n"]

_IMPL_IDS = [name for name, _ in IMPLEMENTATIONS]


def _probe_id(probe: str) -> str:
    if probe == "":
        return "empty"
    if probe.endswith("\n"):
        return "trailing-newline"
    if len(probe) > 20:
        return f"len{len(probe)}"
    return probe


@pytest.mark.parametrize("probe", ACCEPT, ids=[_probe_id(p) for p in ACCEPT])
@pytest.mark.parametrize("impl", [f for _, f in IMPLEMENTATIONS], ids=_IMPL_IDS)
def test_accepts(impl, probe):
    assert impl(probe) is True


@pytest.mark.parametrize("probe", REJECT, ids=[_probe_id(p) for p in REJECT])
@pytest.mark.parametrize("impl", [f for _, f in IMPLEMENTATIONS], ids=_IMPL_IDS)
def test_rejects(impl, probe):
    assert impl(probe) is False


def test_the_boundary_rows_sit_on_the_63_character_ceiling():
    """Named rows, not positions: a row appended later cannot silently
    change what is measured here (blind review)."""
    assert len(_ON_CEILING) == 63 and _ON_CEILING in ACCEPT
    assert len(_ONE_PAST_CEILING) == 64 and _ONE_PAST_CEILING in REJECT


def test_the_three_copies_agree_on_every_probe():
    """Three copies, one answer per probe — a drifted copy shows up here too."""
    disagreements = {}
    for probe in ACCEPT + REJECT:
        answers = {name: bool(fn(probe)) for name, fn in IMPLEMENTATIONS}
        if len(set(answers.values())) != 1:
            disagreements[probe] = answers
    assert not disagreements, disagreements


def test_accept_face_is_not_vacuous():
    """A gate that rejects everything satisfies every REJECT row; this stops it."""
    assert ACCEPT
    for name, fn in IMPLEMENTATIONS:
        assert any(fn(p) for p in ACCEPT), (
            f"{name} accepts none of the ACCEPT probes")


# ── discover level: the gate's rejections must be NAMED on stderr ─────────────

# `.hidden` is an RFC 1123 reject too (leading dot) and IS named on stderr
# by both readers; it is in the list so a change that starts folding
# dot-prefixed stems away silently (instead of naming them) fails here.
_INVALID_STEMS = ("DB-A", "db_a", "db-a-", ".hidden")


def _seed_confd(root: Path) -> Path:
    """conf.d with one valid carrier and four RFC 1123 rejects (one dot-prefixed)."""
    root.mkdir(parents=True, exist_ok=True)
    for stem in ("ok-a",) + _INVALID_STEMS:
        (root / f"{stem}.yaml").write_text(
            f"tenants:\n  {stem}:\n    pg_connections: 90\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("discover", [
    operator_generate.discover_tenant_configs,
    migrate_to_operator.discover_tenant_configs,
], ids=["operator_generate", "migrate_to_operator"])
def test_discover_drops_and_names_every_rfc1123_reject(
        discover, tmp_path, monkeypatch, capsys):
    """#1713: "don't only assert the list excludes it; assert it is named".

    `i18n_text` reads the environment at call time; `DA_LANG` is checked
    first, so pinning it makes the English line the one to grep. The
    `RFC 1123` token appears in both languages as well.
    """
    monkeypatch.setenv("DA_LANG", "en")
    root = _seed_confd(tmp_path / "conf.d")

    tenants = discover(root)
    err = capsys.readouterr().err

    assert tenants == ["ok-a"], tenants
    for stem in _INVALID_STEMS:
        named = [ln for ln in err.splitlines()
                 if f"'{stem}'" in ln
                 and "Skipping invalid tenant name" in ln
                 and "RFC 1123" in ln]
        assert len(named) == 1, (
            f"{stem!r} must be named exactly once as an RFC 1123 reject; "
            f"stderr was {err!r}")
    assert "'ok-a'" not in err, err


@pytest.mark.skipif(sys.platform == "win32",
                    reason="NTFS forbids control characters in file names; "
                           "skipped so local Windows runs match CI Linux")
@pytest.mark.parametrize("discover", [
    operator_generate.discover_tenant_configs,
    migrate_to_operator.discover_tenant_configs,
], ids=["operator_generate", "migrate_to_operator"])
def test_discover_rejects_a_stem_with_a_trailing_newline(
        discover, tmp_path, monkeypatch, capsys):
    """#1779 end to end: a conf.d file literally named `evil\\n.yaml`.

    Measured on `a0c892bb` with the `re.match` gates: both readers returned
    `['evil\\n', 'ok-a']`, i.e. the CR would have been
    `metadata.name: da-tenant-evil\\n`. The file body is valid YAML for a
    tenant `evil`, so the only thing wrong with this carrier is its name.
    `safe_label` prints the newline as `?`, which is what stderr must name.
    """
    monkeypatch.setenv("DA_LANG", "en")
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "ok-a.yaml").write_text(
        "tenants:\n  ok-a:\n    pg_connections: 90\n", encoding="utf-8")
    (root / "evil\n.yaml").write_text(
        "tenants:\n  evil:\n    pg_connections: 90\n", encoding="utf-8")

    tenants = discover(root)
    err = capsys.readouterr().err

    assert tenants == ["ok-a"], tenants
    named = [ln for ln in err.splitlines()
             if "'evil?'" in ln
             and "Skipping invalid tenant name" in ln
             and "RFC 1123" in ln]
    assert len(named) == 1, err
