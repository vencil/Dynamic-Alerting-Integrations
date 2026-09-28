"""A directory symlink in conf.d is named, not dropped in silence (#1972).

kubelet's AtomicWriter projects a ConfigMap `items[].path` that has a
sub-directory (`team-a/x.yaml`) as ONE top-level DIRECTORY link,
`team-a -> ..data/team-a`. Neither walker follows a directory link — the
exporter's `filepath.WalkDir` nor `os.walk(followlinks=False)` — so every
tenant under it is lost. Measured on main before this fix, the loss was
silent everywhere: `validate_config` answered rc 0 / `Result: PASS` for a
tree whose only tenant it never read, and `unusable` was empty.

The fix keeps the walk as it is and makes the loss audible. Two baselines
must not move:

  * the kubelet FLAT layout (file links next to `..data`) — every ConfigMap
    volume looks like that, so any new line there is a false alarm on every
    install;
  * a link to a directory the walk reaches by itself (`current -> team-b`
    beside a real `team-b/`): its tenants ARE loaded through the real path,
    so calling them "NOT loaded" would be false (blind review F1).

The Go twin is `pkg/config/tree_scan_symlink_test.go::
TestScanDirTree_KubeletDirSymlinkIsAudible`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))

from _lib_confd import list_config_tree, unusable_reason  # noqa: E402

VALIDATE = REPO / "scripts" / "tools" / "ops" / "validate_config.py"
ASSEMBLE = REPO / "scripts" / "tools" / "ops" / "assemble_config_dir.py"
DESCRIBE = REPO / "scripts" / "tools" / "dx" / "describe_tenant.py"
PATH_META = REPO / "scripts" / "tools" / "lint" / "check_path_metadata_consistency.py"
COMPILE = REPO / "scripts" / "tools" / "dx" / "compile_custom_alerts.py"

SYMLINK_REASON = ("is a symlink to a directory — threshold-exporter does not "
                  "follow it, so the config files under it are NOT loaded")
TENANT = 'tenants:\n  t-sym:\n    cpu_pct: "70"\n'
DEFAULTS = "defaults:\n  cpu_pct: 50\n"

pytestmark = pytest.mark.skipif(
    not hasattr(os, "symlink") or sys.platform == "win32",
    reason="symlink fixtures need a POSIX runner; CI measures them on ubuntu-latest")


def _kubelet_tree(root: Path, *, nested: bool) -> Path:
    """The layout kubelet builds: payload under `..v1`, `..data -> ..v1`,
    and one top-level link per key — a DIRECTORY link for a nested key."""
    payload = root / "..v1"
    payload.mkdir(parents=True)
    (payload / "_defaults.yaml").write_text(DEFAULTS, encoding="utf-8")
    os.symlink("..v1", root / "..data")
    os.symlink("..data/_defaults.yaml", root / "_defaults.yaml")
    if nested:
        (payload / "team-a").mkdir()
        (payload / "team-a" / "x.yaml").write_text(TENANT, encoding="utf-8")
        os.symlink("..data/team-a", root / "team-a")
    else:
        (payload / "x.yaml").write_text(TENANT, encoding="utf-8")
        os.symlink("..data/x.yaml", root / "x.yaml")
    return root


@pytest.fixture()
def nested(tmp_path: Path) -> Path:
    return _kubelet_tree(tmp_path / "nested", nested=True)


@pytest.fixture()
def flat(tmp_path: Path) -> Path:
    return _kubelet_tree(tmp_path / "flat", nested=False)


@pytest.fixture()
def alias(tmp_path: Path) -> Path:
    """`alias/current -> team-b`, `alias/team-b/x.yaml` a real file."""
    root = tmp_path / "alias-root"
    (root / "alias" / "team-b").mkdir(parents=True)
    (root / "_defaults.yaml").write_text(DEFAULTS, encoding="utf-8")
    (root / "alias" / "team-b" / "x.yaml").write_text(TENANT, encoding="utf-8")
    os.symlink("team-b", root / "alias" / "current")
    return root


@pytest.fixture()
def shared(tmp_path: Path) -> Path:
    """`_shared -> .payload/_shared`: a `_`-prefixed directory link."""
    root = tmp_path / "shared-root"
    (root / ".payload" / "_shared").mkdir(parents=True)
    (root / "_defaults.yaml").write_text(DEFAULTS, encoding="utf-8")
    (root / ".payload" / "_shared" / "x.yaml").write_text(TENANT, encoding="utf-8")
    os.symlink(".payload/_shared", root / "_shared")
    return root


def _run(*argv: object) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *map(str, argv)],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False, timeout=120)


# ── the listing ───────────────────────────────────────────────────────


def test_list_config_tree_names_the_directory_link(nested: Path):
    listing = list_config_tree(nested)
    assert [p.name for p in listing.unusable] == ["team-a"], (
        "a directory symlink went missing from `unusable` — the tenants "
        "under it are lost with no signal again")
    assert [p.name for p in listing.files] == ["_defaults.yaml"]
    assert unusable_reason(listing.unusable[0]) == SYMLINK_REASON


def test_list_config_tree_flat_kubelet_layout_is_unchanged(flat: Path):
    listing = list_config_tree(flat)
    assert [p.name for p in listing.files] == ["_defaults.yaml", "x.yaml"]
    assert listing.unusable == [], (
        "the kubelet flat layout (and its `..data` link) must not be "
        "reported: that is every ConfigMap volume")
    assert listing.unscannable == []


def test_list_config_tree_alias_to_a_walked_directory_is_not_unusable(alias: Path):
    listing = list_config_tree(alias)
    assert [p.relative_to(alias).as_posix() for p in listing.files] == [
        "_defaults.yaml", "alias/team-b/x.yaml"]
    assert listing.unusable == [], (
        "a link to a directory the walk reaches anyway loses nothing")


def test_list_config_tree_link_outside_the_root_is_unusable(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "x.yaml").write_text(TENANT, encoding="utf-8")
    os.symlink(tmp_path / "elsewhere", root / "ext")
    assert [p.name for p in list_config_tree(root).unusable] == ["ext"]


# ── validate_config ───────────────────────────────────────────────────


def test_validate_config_fails_and_does_not_contradict_itself(nested: Path):
    r = _run(VALIDATE, "--config-dir", nested)
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    assert "[FAIL] yaml_syntax" in out
    assert f"team-a: {SYMLINK_REASON} — project those files" in out
    # The pre-fix consequence walked INTO the link and said the opposite,
    # and the first fix repeated the loss twice in one line (N3).
    assert "ARE loaded" not in out, out
    assert "nothing in it is loaded" not in out, out


def test_validate_config_flat_kubelet_layout_still_passes(flat: Path, amtool_accepts):
    # #2311: without an amtool the routes row is WARN by design.
    r = _run(VALIDATE, "--config-dir", flat)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "Result: PASS" in out
    assert "symlink" not in out


def test_validate_config_alias_still_passes(alias: Path):
    r = _run(VALIDATE, "--config-dir", alias)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "symlink" not in out


# ── assemble_config_dir --check ───────────────────────────────────────


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "o.yaml").write_text('tenants:\n  t-other:\n    cpu_pct: "60"\n',
                                encoding="utf-8")
    return src


def test_assemble_check_does_not_call_a_directory_link_unmeasured(
        nested: Path, tmp_path: Path):
    r = _run(ASSEMBLE, "--sources", _source(tmp_path), "--output", nested,
             "--check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cannot answer the duplicate-tenant question" not in r.stderr
    assert f"WARN: {nested / 'team-a'} {SYMLINK_REASON}\n" in r.stderr


def test_assemble_check_flat_kubelet_layout_is_silent(flat: Path, tmp_path: Path):
    r = _run(ASSEMBLE, "--sources", _source(tmp_path), "--output", flat,
             "--check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARN" not in r.stderr and "symlink" not in r.stderr


# ── the readers that only report (rc unchanged) ───────────────────────


def test_describe_tenant_and_path_metadata_name_it_consistently(nested: Path):
    d = _run(DESCRIBE, "--conf-d", nested, "t-sym")
    assert d.returncode == 2  # t-sym is not loaded — same as the exporter
    # Once: describe_tenant and the custom-alerts loader it runs both see it.
    assert (d.stdout + d.stderr).count(f"team-a — {SYMLINK_REASON}") == 1, d.stderr
    m = _run(PATH_META, "--config-dir", nested)
    assert m.returncode == 0
    assert SYMLINK_REASON in m.stdout + m.stderr


def test_describe_tenant_and_path_metadata_flat_are_unchanged(flat: Path):
    d = _run(DESCRIBE, "--conf-d", flat, "t-sym")
    assert d.returncode == 0, d.stdout + d.stderr
    assert "symlink" not in d.stdout + d.stderr
    m = _run(PATH_META, "--config-dir", flat)
    assert m.returncode == 0
    assert "warning" not in m.stdout + m.stderr


def test_underscore_directory_link_is_named_by_name_filtering_readers(shared: Path):
    """F3: both readers drop `_`-prefixed ENTRIES by name; a `_`-prefixed
    directory LINK is a lost subtree (the exporter descends `_` dirs) and
    must be named like `unscannable` is."""
    d = _run(DESCRIBE, "--conf-d", shared, "t-sym")
    assert f"_shared — {SYMLINK_REASON}" in d.stdout + d.stderr, d.stderr
    m = _run(PATH_META, "--config-dir", shared)
    assert m.returncode == 0
    assert f"_shared:0: warning: not checked — {SYMLINK_REASON}" in m.stdout + m.stderr


def test_compile_custom_alerts_allow_empty_is_not_refused(nested: Path, tmp_path: Path):
    """F2: a directory link is not a quarantined recipe; `--allow-empty`
    must still write (rc 0, as on main) and the link must be named."""
    out = tmp_path / "pack.yaml"
    r = _run(COMPILE, "--config-dir", nested, "--out", out, "--allow-empty")
    both = r.stdout + r.stderr
    assert r.returncode == 0, both
    assert "QUARANTINED" not in both, both
    assert f"team-a — {SYMLINK_REASON}" in both
    assert out.is_file()


def test_compile_custom_alerts_flat_is_unchanged(flat: Path, tmp_path: Path):
    r = _run(COMPILE, "--config-dir", flat, "--out", tmp_path / "pack.yaml",
             "--allow-empty")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "symlink" not in r.stdout + r.stderr
