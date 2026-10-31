"""test_routing_unknown_keys.py — `_routing`-prefixed keys the route generator
does not read in a tenant's config or at the top level of a `_` file (#2983).

`_routing` is a reserved PREFIX, so `_routing_profle` passed every key check
as reserved while nothing read it. This file is the only reader of the
shared case table.
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(REPO, "scripts", "tools"))
sys.path.insert(0, os.path.join(REPO, "scripts", "tools", "ops"))

import _grar_validate as gv  # noqa: E402
from _grar_merge import IgnoredKeyWarning  # noqa: E402
from _lib_exitcodes import emit_result  # noqa: E402  (#3216: in-process main() → exit status)


def _as_run(result):
    """What `_lib_exitcodes.run` does with main()'s answer (#3216): a Report's
    document is written and its status is the exit, as SystemExit."""
    raise SystemExit(emit_result(result))

GEN = os.path.join(REPO, "scripts", "tools", "ops", "generate_alertmanager_routes.py")

with open(os.path.join(REPO, "tests", "shared", "routing_unknown_keys_cases.json"),
          encoding="utf-8") as _f:
    CASES = json.load(_f)["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_shared_cases(case):
    got = gv.unknown_routing_keys(case["value"], layer=case["layer"], where="x")
    assert all(isinstance(w, IgnoredKeyWarning) and w.kind == "routing_key_ignored"
               for w in got)
    assert sorted(w.field for w in got) == sorted(case["keys"])


def test_non_string_keys_are_passed_over():
    assert gv.unknown_routing_keys({True: 1, 5: 2, None: 3}, layer="tenant", where="x") == []


@pytest.mark.parametrize("key,layer,hint", [
    ("_routing_profle", "tenant", "did you mean '_routing_profile'?"),
    ("_routing_defualts", "platform", "did you mean '_routing_defaults'?"),
    ("_routing_defaults", "tenant", "a platform key"),
    ("_routing", "platform", "a tenant key"),
    ("_routing_receiver_type", "tenant", "`_routing.receiver.type`"),
    ("_routing_mode", "tenant", "read here: ['_routing', '_routing_profile']"),
])
def test_message_names_the_key_and_a_hint(key, layer, hint):
    (w,) = gv.unknown_routing_keys({key: 1}, layer=layer, where="w", file="f.yaml")
    assert w.startswith(f"  WARN: w in f.yaml: '{key}' is not a key the route generator reads (")
    assert hint in w and w.endswith("; it has no effect")


# A tree that renders the same whether or not each stray key is there: the
# tenant file, its root platform `tenants:` entry, the root carrier, another
# root `_` file, a nested carrier. `{X}` slots take one stray line each.
_CLEAN = {
    "_defaults.yaml": ("defaults:\n  cpu_usage_percent: 80\n_routing_defaults:\n"
                       "  receiver:\n    type: webhook\n    url: https://hooks.example.com/d\n"
                       "{D}tenants:\n  t1:\n    _severity_dedup: disable\n{O}"),
    "_routing_profiles.yaml": ("routing_profiles:\n  p1:\n    receiver:\n      type: webhook\n"
                               "      url: https://hooks.example.com/p\n{P}"),
    "t1.yaml": "tenants:\n  t1:\n    cpu_usage_percent: '50'\n{T}",
    "team/_defaults.yaml": "defaults:\n  cpu_usage_percent: 70\n{N}",
    "team/t3.yaml": "tenants:\n  t3:\n    cpu_usage_percent: '50'\n",
}
# place → (stray line, file named, key named, tenant named)
_STRAY = {"T": ("    _routing_profle: p1\n", None, "_routing_profle", "t1"),
          "O": ("    _routing_receiver_type: slack\n", None, "_routing_receiver_type", "t1"),
          "D": ("_routing_defualts:\n  group_wait: 10s\n", "_defaults.yaml", "_routing_defualts", None),
          "P": ("_routing_profile: p1\n", "_routing_profiles.yaml", "_routing_profile", None),
          "N": ("_routing_enforce:\n  enabled: true\n", "team/_defaults.yaml", "_routing_enforce", None)}


def _tree(root, stray=()):
    for rel, text in _CLEAN.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.format(**{k: (_STRAY[k][0] if k in stray else "") for k in _STRAY}),
                        encoding="utf-8")
    return root


def _gen(config_dir, *args):
    return subprocess.run([sys.executable, GEN, "--config-dir", str(config_dir), *args],
                          capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)


@pytest.mark.parametrize("place", sorted(_STRAY))
def test_each_place_is_named_and_blocks_validate_only(place, tmp_path):
    clean = _tree(tmp_path / "clean")
    dirty = _tree(tmp_path / "dirty", stray=(place,))
    _, file, key, tenant = _STRAY[place]
    # Render mode: rc 0, the key named, the output as if it were absent.
    rc_clean = _gen(clean, "-o", str(tmp_path / "clean.yaml"))
    rc_dirty = _gen(dirty, "-o", str(tmp_path / "dirty.yaml"))
    assert rc_clean.returncode == 0 and rc_dirty.returncode == 0, rc_dirty.stderr
    assert (tmp_path / "clean.yaml").read_bytes() == (tmp_path / "dirty.yaml").read_bytes()
    assert f"'{key}' is not a key the route generator reads" in rc_dirty.stderr
    assert "is not a key the route generator reads" not in rc_clean.stderr
    # --validate (and --validate --strict): the dirty tree fails, the clean one passes.
    assert _gen(clean, "--validate", "--strict").returncode == 0
    for mode in (("--validate",), ("--validate", "--strict")):
        v = _gen(dirty, *mode)
        assert v.returncode == 1, (mode, v.stdout + v.stderr)
    out = tmp_path / "findings.json"
    _gen(dirty, "--validate", "--findings-json", str(out))
    recs = [r for r in json.loads(out.read_text(encoding="utf-8"))["findings"]
            if r["kind"] == "routing_key_ignored"]
    assert [(r["file"], r["field"], r["tenant"], r["blocks"]) for r in recs] == \
        [(file, key, tenant, "validate")]


def test_a_tenant_files_top_level_is_not_a_platform_file(tmp_path):
    """Only a `_` file's top level is read for platform keys; a tenant file's
    stray top-level key is another reader's to name, and an unselected
    carrier spelling is skipped whole (da-guard skips it too)."""
    root = _tree(tmp_path / "t")
    with open(root / "t1.yaml", "a", encoding="utf-8") as f:
        f.write("_routing_defualts: {}\n")
    (root / "team" / "_defaults.yml").write_text("_routing_enforce: {}\n", encoding="utf-8")
    err = _gen(root, "--validate").stderr
    assert "_routing_defualts" not in err and "_routing_enforce" not in err


@pytest.mark.usefixtures("da_guard_env")
def test_validate_config_schema_row_fails(tmp_path, capsys, cli_argv):
    import validate_config as vc
    d = _tree(tmp_path / "d", stray=("T",))
    cli_argv("validate_config", "--config-dir", str(d), "--json")
    with pytest.raises(SystemExit) as exc:
        _as_run(vc.main())
    rows = {r["check"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["schema"]["status"] == "fail", rows["schema"]
    assert any("'_routing_profle' is not a key the route generator reads" in x
               for x in rows["schema"]["details"]), rows["schema"]
    assert exc.value.code == 1
