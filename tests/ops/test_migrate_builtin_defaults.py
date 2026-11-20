"""migrate-builtin-defaults (#3832 decision 7, branch 5).

Acceptance from the design: values that differ from the built-in one are kept;
an operator key equal to the registry's value is kept (it has no built-in
value); `--keep` keys are kept; deletions are refused when an exporter digest
differs; a root `null` or `optional_overrides:` listing rewritten to "disable"
stops serving the key. And the property the tool exists for: removing the copies
changes no row /metrics serves. Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(_REPO / "scripts" / "tools"))
sys.path.insert(0, str(_REPO / "tests"))

import _lib_tenant_values as tv  # noqa: E402
import migrate_builtin_defaults as mig  # noqa: E402
from _builtin_layer import BUILTIN_DEFAULTS  # noqa: E402

_DIGEST = None


def _digest(conf: Path) -> str:
    global _DIGEST
    if _DIGEST is None:
        _DIGEST = tv.load_effective_tree(conf).builtin_defaults.digest
    return _DIGEST


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    conf = tmp_path / "conf.d"
    for rel, text in files.items():
        p = conf / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return conf


def _served(conf: Path) -> dict:
    out = {}
    for tenant, t in tv.load_served_values(conf, at=mig._AT).items():
        out[tenant] = {k: mig._key_state(t, k) for k in set(t.values) | set(t.series)}
    return out


def _load(conf: Path, rel: str) -> dict:
    return yaml.safe_load((conf / rel).read_text(encoding="utf-8")) or {}


# A tree written before the built-in layer: the root copies two built-in keys
# (pg_connections = the platform's 80, container_cpu at its own 70), and an
# operator key (redis_connected_clients, no built-in value) at the registry's
# 200; t1 repeats the inherited 80, t2 writes its own 90.
_OLD = {
    "_defaults.yaml": "defaults:\n  pg_connections: 80\n  container_cpu: 70\n"
                      "  redis_connected_clients: 200\n",
    "t1.yaml": "tenants:\n  t1:\n    pg_connections: \"80\"\n",
    "t2.yaml": "tenants:\n  t2:\n    pg_connections: \"90\"\n",
}


def test_the_fixture_keys_are_what_the_test_assumes():
    assert BUILTIN_DEFAULTS["pg_connections"] == 80
    assert BUILTIN_DEFAULTS["container_cpu"] != 70
    assert "redis_connected_clients" not in BUILTIN_DEFAULTS


def test_only_copies_are_deleted_and_nothing_served_changes(tmp_path, capsys):
    conf = _tree(tmp_path, _OLD)
    before = _served(conf)
    rc = mig.main(["--config-dir", str(conf), "--apply", "--exporter-digest", _digest(conf)])
    assert rc == 0, capsys.readouterr()
    root = _load(conf, "_defaults.yaml")["defaults"]
    assert "pg_connections" not in root                       # a copy: deleted
    assert root["container_cpu"] == 70                        # its own value: kept
    assert root["redis_connected_clients"] == 200             # operator key: kept
    assert "pg_connections" not in _load(conf, "t1.yaml")["tenants"]["t1"]   # copy
    assert _load(conf, "t2.yaml")["tenants"]["t2"]["pg_connections"] == "90"  # own
    assert _served(conf) == before


def test_preview_writes_nothing(tmp_path, capsys):
    conf = _tree(tmp_path, _OLD)
    texts = {p: p.read_bytes() for p in conf.rglob("*.yaml")}
    assert mig.main(["--config-dir", str(conf)]) == 0
    out = capsys.readouterr().out
    assert "delete _defaults.yaml: defaults.pg_connections" in out
    assert "delete t1.yaml: tenants.t1.pg_connections" in out
    assert "container_cpu" not in out.split("copies")[1].split("switched")[0]
    assert {p: p.read_bytes() for p in conf.rglob("*.yaml")} == texts


def test_keep_leaves_a_key_alone(tmp_path):
    conf = _tree(tmp_path, _OLD)
    assert mig.main(["--config-dir", str(conf), "--apply", "--exporter-digest", _digest(conf),
                     "--keep", "pg_connections"]) == 0
    assert _load(conf, "_defaults.yaml")["defaults"]["pg_connections"] == 80
    assert _load(conf, "t1.yaml")["tenants"]["t1"]["pg_connections"] == "80"


@pytest.mark.parametrize("digests,rc", [(["0000deadbeef"], 1), ([], 0)],
                         ids=["a-differing-digest", "no-digest"])
def test_deletions_need_every_exporters_digest(tmp_path, capsys, digests, rc):
    conf = _tree(tmp_path, _OLD)
    texts = {p: p.read_bytes() for p in conf.rglob("*.yaml")}
    args = ["--config-dir", str(conf), "--apply"]
    for d in digests:
        args += ["--exporter-digest", d]
    assert mig.main(args) == rc
    assert {p: p.read_bytes() for p in conf.rglob("*.yaml")} == texts
    err = capsys.readouterr().err
    assert ("differ from this tool's built-in table" if digests else "needs --exporter-digest") in err


def test_one_matching_digest_does_not_cover_a_cluster_that_differs(tmp_path):
    conf = _tree(tmp_path, _OLD)
    assert mig.main(["--config-dir", str(conf), "--apply", "--exporter-digest", _digest(conf),
                     "--exporter-digest", "0000deadbeef"]) == 1
    assert _load(conf, "_defaults.yaml")["defaults"]["pg_connections"] == 80


def test_a_value_that_overrides_another_layer_is_not_a_copy(tmp_path, capsys):
    """t1's 80 equals the built-in value, but it overrides team/_defaults.yaml's
    70: deleting it would change t1's row, so it is kept and named — an
    override, not a failure (rc 0)."""
    conf = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  redis_connected_clients: 200\n",
        "team/_defaults.yaml": "defaults:\n  pg_connections: 70\n",
        "team/t1.yaml": "tenants:\n  t1:\n    pg_connections: \"80\"\n",
    })
    before = _served(conf)
    assert mig.main(["--config-dir", str(conf), "--apply", "--exporter-digest", _digest(conf)]) == 0
    assert "kept team/t1.yaml: tenants.t1.pg_connections" in capsys.readouterr().out
    assert _load(conf, "team/t1.yaml")["tenants"]["t1"]["pg_connections"] == "80"
    assert _served(conf) == before


@pytest.mark.parametrize("root", [
    "defaults:\n  pg_connections: null\n",
    "optional_overrides:\n  - pg_connections\n  - mysql_threads_running\n",
], ids=["root-null", "optional-overrides-listing"])
def test_the_old_ways_of_switching_off_become_disable(tmp_path, root):
    """Without a digest (before the upgrade, decision 9): the rewrite is not
    gated. After it the key is off for every tenant, and a declared key that is
    not built in stays on the list."""
    conf = _tree(tmp_path, {"_defaults.yaml": root,
                            "t1.yaml": "tenants:\n  t1:\n    container_cpu: \"70\"\n"})
    assert "pg_connections" in _served(conf)["t1"]            # the built-in layer serves it
    assert mig.main(["--config-dir", str(conf), "--apply"]) == 0
    data = _load(conf, "_defaults.yaml")
    assert data["defaults"]["pg_connections"] == "disable"
    assert "pg_connections" not in (data.get("optional_overrides") or [])
    if "optional_overrides" in root:
        assert data["optional_overrides"] == ["mysql_threads_running"]
    assert "pg_connections" not in _served(conf)["t1"]


def test_a_tree_with_nothing_to_do_writes_nothing(tmp_path, capsys):
    conf = _tree(tmp_path, {"t1.yaml": "tenants:\n  t1:\n    container_cpu: \"70\"\n"})
    assert mig.main(["--config-dir", str(conf), "--apply", "--exporter-digest", _digest(conf)]) == 0
    assert "nothing written" in capsys.readouterr().out


def test_a_missing_config_dir_is_a_caller_error(tmp_path):
    """Refused naming the flag (`_lib_exitcodes.run` turns it into rc 2)."""
    import _lib_exitcodes
    with pytest.raises(_lib_exitcodes.CallerError, match="--config-dir"):
        mig.main(["--config-dir", str(tmp_path / "nope")])


@pytest.mark.parametrize("root", [
    "optional_overrides:\n  - pg_connections\n",
    "defaults:\n  pg_connections: null\n",
], ids=["listed", "null"])
def test_a_tenant_value_that_keeps_a_switched_off_key_on_is_not_a_copy(tmp_path, root):
    """Review finding: the root switched the key off and t1 turned it back on with
    its own 80 (the platform's value). In the same run the root becomes "disable",
    so deleting t1's 80 would switch t1 off: the proof runs on top of the
    rewrites, and t1's value is kept."""
    conf = _tree(tmp_path, {"_defaults.yaml": root,
                            "t1.yaml": "tenants:\n  t1:\n    pg_connections: \"80\"\n"})
    assert mig.main(["--config-dir", str(conf), "--apply", "--exporter-digest", _digest(conf)]) == 0
    assert _load(conf, "t1.yaml")["tenants"]["t1"]["pg_connections"] == "80"
    assert _load(conf, "_defaults.yaml")["defaults"]["pg_connections"] == "disable"
    assert "pg_connections" in _served(conf)["t1"]


def test_one_candidate_that_is_not_a_copy_does_not_keep_the_others(tmp_path, capsys):
    """Review finding: t2's 80 overrides a subdirectory's 70 (not a copy), but the
    root's 80 and t1's 80 are copies on their own: those are deleted, t2's kept."""
    conf = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  pg_connections: 80\n",
        "t1.yaml": "tenants:\n  t1:\n    pg_connections: \"80\"\n",
        "team/_defaults.yaml": "defaults:\n  pg_connections: 70\n",
        "team/t2.yaml": "tenants:\n  t2:\n    pg_connections: \"80\"\n",
    })
    before = _served(conf)
    assert mig.main(["--config-dir", str(conf), "--apply", "--exporter-digest", _digest(conf)]) == 0
    assert "pg_connections" not in (_load(conf, "_defaults.yaml").get("defaults") or {})
    assert "pg_connections" not in _load(conf, "t1.yaml")["tenants"]["t1"]
    assert _load(conf, "team/t2.yaml")["tenants"]["t2"]["pg_connections"] == "80"
    assert "kept team/t2.yaml: tenants.t2.pg_connections" in capsys.readouterr().out
    assert _served(conf) == before


def test_a_symlinked_tenant_file_is_named_not_a_crash(tmp_path, capsys):
    """Review finding: da-guard write does not write through a symlink, so a copy
    in a symlinked file is kept and named; the preview and the run still work."""
    outside = tmp_path / "outside.yaml"
    outside.write_text("tenants:\n  t1:\n    pg_connections: \"80\"\n", encoding="utf-8")
    conf = _tree(tmp_path, {"_defaults.yaml": "defaults:\n  pg_connections: 80\n"})
    (conf / "t1.yaml").symlink_to(outside)
    assert mig.main(["--config-dir", str(conf)]) == 0
    out = capsys.readouterr().out
    assert "kept t1.yaml: tenants.t1.pg_connections" in out and "symlink" in out
    assert mig.main(["--config-dir", str(conf), "--apply", "--exporter-digest", _digest(conf)]) == 0
    assert "pg_connections" not in (_load(conf, "_defaults.yaml").get("defaults") or {})
    assert outside.read_text(encoding="utf-8").count("80") == 1


def test_the_other_listed_items_are_never_retyped(tmp_path, capsys):
    """Review finding: the list used to be rewritten through Python `str()`, so a
    `1e2` item came back as `100.0`. The kept items are now re-emitted as the
    reader read them; one the edit cannot write back exactly as written is a
    named refusal (rc 2, nothing written), never a silent respelling."""
    conf = _tree(tmp_path, {"_defaults.yaml":
                            "optional_overrides:\n  - pg_connections\n  - mysql_threads_running\n"})
    assert mig.main(["--config-dir", str(conf), "--apply"]) == 0
    text = (conf / "_defaults.yaml").read_text(encoding="utf-8")
    assert "- pg_connections" not in text and "mysql_threads_running" in text, text
    odd = _tree(tmp_path / "odd", {"_defaults.yaml":
                                   "optional_overrides:\n  - pg_connections\n  - 1e2\n"})
    before = (odd / "_defaults.yaml").read_bytes()
    assert mig.main(["--config-dir", str(odd), "--apply"]) == 2
    assert "1e2" in capsys.readouterr().err
    assert (odd / "_defaults.yaml").read_bytes() == before
