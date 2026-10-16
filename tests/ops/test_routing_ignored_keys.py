"""test_routing_ignored_keys.py — keys a routing block carries that the route
generator does not read (#2960).

The key sets are what the generator reads (``_grar_validate``); the JSON
schemas are pinned to them here, not the other way round. This file is the
only reader of the shared case table.
"""
import json
import os
import sys

import pytest

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(REPO, "scripts", "tools"))
sys.path.insert(0, os.path.join(REPO, "scripts", "tools", "ops"))

import _grar_validate as gv  # noqa: E402
from _grar_merge import IgnoredKeyWarning  # noqa: E402
from _lib_python import RECEIVER_TYPES  # noqa: E402

with open(os.path.join(REPO, "tests", "shared", "routing_ignored_keys_cases.json"),
          encoding="utf-8") as _f:
    CASES = json.load(_f)["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_shared_cases(case):
    got = gv.ignored_routing_keys(case["value"], block=case["block"], where="x",
                                  field_prefix="p.")
    assert all(isinstance(w, IgnoredKeyWarning) and w.kind == "routing_key_ignored"
               for w in got)
    assert sorted(w.field for w in got) == sorted("p." + f for f in case["fields"])


def _schema_props(name):
    with open(os.path.join(REPO, "docs", "schemas", "tenant-config.schema.json"),
              encoding="utf-8") as f:
        d = json.load(f)["definitions"][name]
    assert d.get("additionalProperties") is False, name
    return set(d["properties"])


@pytest.mark.parametrize("keys,definition", [
    (gv.ROUTING_BLOCK_KEYS, "routing"),
    (gv.ROUTING_DEFAULTS_KEYS - {"routes"}, "routingDefaults"),
    (gv.OVERRIDE_ENTRY_KEYS, "routingOverride"),
    (gv.ROUTING_ENFORCED_KEYS, "routingEnforced"),
])
def test_schema_is_pinned_to_the_generators_reads(keys, definition):
    assert _schema_props(definition) == set(keys)


@pytest.mark.parametrize("rtype", sorted(RECEIVER_TYPES))
def test_receiver_keys_cover_every_field_the_spec_names(rtype):
    spec = RECEIVER_TYPES[rtype]
    keys = gv.receiver_keys(rtype)
    named = {"type", *spec.get("required", ()), *spec.get("optional", ()),
             *spec.get("metadata", ())}
    for group in spec.get("exactly_one_of", ()):
        named |= set(group)
    assert keys == named
    assert gv.receiver_keys("no-such-type") is None


def test_non_string_keys_are_passed_over():
    """The routing loader reads keys as text and drops a null key's pair, so a
    non-string key reaches here only from another reader; it is passed over."""
    got = gv.ignored_routing_keys({True: 1, "receiver": {"type": "webhook", "url": "u", 5: 1}},
                                  block="routing", where="x")
    assert got == []


def test_loader_key_text_is_named(tmp_path):
    """`1:` / `on:` are keys the generator does not read; `~:` is dropped."""
    import _grar_parse as gp
    (tmp_path / "_defaults.yaml").write_text("defaults:\n  cpu: 70\n", encoding="utf-8")
    (tmp_path / "tx.yaml").write_text(
        "tenants:\n  tx:\n    cpu: '80'\n    _routing:\n      on: x\n      1: y\n      ~: z\n"
        "      receiver: {type: webhook, url: 'https://t.example/g'}\n", encoding="utf-8")
    tree = gp.load_tenant_tree(str(tmp_path))
    got = sorted(w.field for w in tree.schema_warnings if isinstance(w, IgnoredKeyWarning))
    assert got == ["1", "on"]


@pytest.mark.parametrize("key,block,hint", [
    ("continue", "routing", "an Alertmanager route field the generator does not render"),
    ("recevier", "routing", "did you mean 'receiver'?"),
    ("zzz", "routing_enforced", "read here: ["),
])
def test_message_names_the_key_and_a_hint(key, block, hint):
    (w,) = gv.ignored_routing_keys({key: 1}, block=block, where="t1: _routing")
    assert w.startswith(f"  WARN: t1: _routing: '{key}' is not read by the route generator (")
    assert hint in w


def test_override_match_hint():
    (w,) = gv.ignored_routing_keys({"overrides": [{"alertname": "A", "matchers": []}]},
                                   block="routing", where="t1: _routing")
    assert "t1: _routing.overrides[0]: 'matchers'" in w
    assert "an override matches by 'alertname' or 'metric_group'" in w


# ── the generator and validate-config, end to end ─────────────────────────
GEN = os.path.join(REPO, "scripts", "tools", "ops", "generate_alertmanager_routes.py")

_CLEAN = {
    "_defaults.yaml": ("defaults:\n  cpu: 70\n_routing_defaults:\n  receiver:\n    type: webhook\n"
                       "    url: https://hooks.example.com/d\n{D}"
                       "_routing_enforced:\n  enabled: true\n  receiver:\n    type: webhook\n"
                       "    url: https://hooks.example.com/noc\n{E}"),
    "_routing_profiles.yaml": ("routing_profiles:\n  p1:\n    receiver:\n      type: webhook\n"
                               "      url: https://hooks.example.com/p\n{P}"),
    "t1.yaml": "tenants:\n  t1:\n    cpu: '50'\n    _routing_profile: p1\n",
    "t2.yaml": ("tenants:\n  t2:\n    cpu: '50'\n    _routing:\n      receiver:\n        type: webhook\n"
                "        url: https://hooks.example.com/t2\n{T}"),
    "team/_defaults.yaml": "defaults:\n  cpu: 60\n_routing_defaults:\n  group_wait: 30s\n{N}",
    "team/t3.yaml": "tenants:\n  t3:\n    cpu: '50'\n",
}
# One stray key per place the generator reads a routing block.
_STRAY = {"D": ("  continue: true\n", "_defaults.yaml", "_routing_defaults.continue"),
          "E": ("  mute_time_intervals: [weekends]\n", "_defaults.yaml",
                "_routing_enforced.mute_time_intervals"),
          "P": ("    active_time_intervals: [office]\n", "_routing_profiles.yaml",
                "routing_profiles.p1.active_time_intervals"),
          "T": ("      continue: true\n", None, "continue"),
          "N": ("  matchers: []\n", "team/_defaults.yaml", "_routing_defaults.matchers")}


def _tree(root, stray=()):
    for rel, text in _CLEAN.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.format(**{k: (_STRAY[k][0] if k in stray else "") for k in _STRAY}),
                        encoding="utf-8")
    return root


def _gen(config_dir, *args):
    import subprocess
    return subprocess.run([sys.executable, GEN, "--config-dir", str(config_dir), *args],
                          capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)


@pytest.mark.parametrize("place", sorted(_STRAY))
def test_each_place_is_named_and_blocks_validate_only(place, tmp_path):
    clean = _tree(tmp_path / "clean")
    dirty = _tree(tmp_path / "dirty", stray=(place,))
    _, file, field = _STRAY[place]
    # Render mode: rc 0, the key named, the output as if it were absent.
    rc_clean = _gen(clean, "-o", str(tmp_path / "clean.yaml"))
    rc_dirty = _gen(dirty, "-o", str(tmp_path / "dirty.yaml"))
    assert rc_clean.returncode == 0 and rc_dirty.returncode == 0, rc_dirty.stderr
    assert (tmp_path / "clean.yaml").read_bytes() == (tmp_path / "dirty.yaml").read_bytes()
    key = field.rsplit(".", 1)[-1]
    assert f"'{key}' is not read by the route generator" in rc_dirty.stderr
    assert "is not read by the route generator" not in rc_clean.stderr
    # --validate: the dirty tree fails, the clean one passes.
    assert _gen(clean, "--validate").returncode == 0
    v = _gen(dirty, "--validate")
    assert v.returncode == 1, v.stdout + v.stderr
    # The finding's own fields, as --findings-json writes them.
    out = tmp_path / "findings.json"
    _gen(dirty, "--validate", "--findings-json", str(out))
    recs = [r for r in json.loads(out.read_text(encoding="utf-8"))["findings"]
            if r["kind"] == "routing_key_ignored"]
    assert [(r["file"], r["field"], r["blocks"]) for r in recs] == [(file, field, "validate")]
    assert recs[0]["tenant"] == ("t2" if place == "T" else None)


@pytest.mark.usefixtures("da_guard_env")
def test_validate_config_rows_fail(tmp_path, capsys, cli_argv):
    import validate_config as vc
    d = _tree(tmp_path / "d", stray=("T",))
    cli_argv("validate_config", "--config-dir", str(d), "--json")
    with pytest.raises(SystemExit) as exc:
        vc.main()
    rows = {r["check"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["schema"]["status"] == "fail", rows["schema"]
    assert any("'continue' is not read by the route generator" in x
               for x in rows["schema"]["details"]), rows["schema"]
    assert exc.value.code == 1


def test_file_follows_the_path_in_the_message():
    """`_routing_defaults.overrides[0] in _defaults.yaml`, never
    `… in _defaults.yaml.overrides[0]` (#2960 review)."""
    (w,) = gv.ignored_routing_keys({"overrides": [{"alertname": "A", "matchers": []}]},
                                   block="routing_defaults", where="_routing_defaults",
                                   file="_defaults.yaml")
    assert w.startswith("  WARN: _routing_defaults.overrides[0] in _defaults.yaml: 'matchers'")
