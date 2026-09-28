#!/usr/bin/env python3
"""A tool that rewrites a conf.d file leaves the values it did not touch
as the exporter read them (#2220).

`deprecate_rule --execute` (Step 1 `_defaults.yaml` carriers, Step 2 tenant
files) and `patch_config` (`_patch_carrier`, legacy and multi-file) load a
file, delete or set ONE key and dump the rest back. Through
`safe_load` + `safe_dump` every other unquoted value was retyped on the way:
`010` → `8`, `12:30` → `750`, `0x1F` → `31`, `yes` → `true`,
`2099-01-01T00:00:00Z` → `2099-01-01 00:00:00+00:00` — and the exporter's
value moved with it (10 → 8, 12 → 750, a maintenance window closed).

⛔ The oracle is the exporter, not a Python reader: `da-guard served-values`
(what /metrics serves) before and after the write must be the same except
for the one key the tool was asked to change. Typed fields
(`defaults` float64, `max_metrics_per_tenant` int, `min_events` strictInt,
`_routing` bools) are in the tree because the obvious other fix — keep every
scalar as a string — would quote them, and the exporter then refuses the
whole file (`parse_failed`, which `load_served_values` raises on).

No `go` → skip with the reason; `VIBE_REQUIRE_GO=1` (CI's python-tests-run)
makes a missing `go` a failure instead, as in `test_patch_config_exporter`.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import _lib_tenant_values as tv
import deprecate_rule
import patch_config as pc
from _lib_io import (DuplicateKeyError, load_yaml_file_for_rewrite,
                     strict_load_for_rewrite)
from _lib_yaml_keys import RawPlain, dump_for_rewrite, load_for_rewrite

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "components" / "threshold-exporter" / "app"
DEPRECATE = Path(deprecate_rule.__file__).resolve()
AT = "2026-09-28T12:00:00Z"
_HAS_GO = shutil.which("go") is not None
_REQUIRE_GO = os.environ.get("VIBE_REQUIRE_GO") == "1"
needs_go = pytest.mark.skipif(
    not _HAS_GO, reason="go not on PATH: cannot build da-guard, the oracle "
    "these tests compare against (set up Go, or VIBE_REQUIRE_GO=1 to fail "
    "instead)")

# ── the tree ─────────────────────────────────────────────────────────────
#
# One tenant per file, so patch_config's multi-file mode (one tenant per
# key) can rewrite each. Every tenant carries `mysql_slave_lag`, so
# deprecate_rule's Step 2 rewrites every file.

_DEFAULTS = """\
# platform carrier
defaults:
  mysql_connections: 80
  mysql_slave_lag: 30
  mysql_threads_running: 010
  container_cpu: 80.5
max_metrics_per_tenant: 500
_routing_defaults:
  receiver:
    type: email
    to: [dba@example.com]
    send_resolved: true
  group_wait: 30s
"""

_TENANT_BODIES = {
    # tenant id (as the exporter reads it) → (key as written, body lines)
    "t-oct": ("t-oct", "    mysql_connections: 010\n"),
    "t-clock": ("t-clock", "    mysql_connections: 12:30\n"),
    "t-hex": ("t-hex", "    mysql_connections: 0x1F\n"),
    "t-yes": ("t-yes", "    mysql_connections: 70\n    _severity_dedup: yes\n"),
    "t-quoted": ("t-quoted", "    mysql_connections: '010'\n"),
    "t-typed": ("t-typed", """\
    mysql_connections: 70
    _state_maintenance:
      target: enable
      expires: 2099-01-01T00:00:00Z
      reason: plain-timestamp
    _metadata:
      owner: team-a
      tier: 1
    _routing:
      receiver:
        type: webhook
        url: https://t.example.com/hook
        send_resolved: false
      group_wait: 30s
    _custom_alerts:
      - recipe: slo_burn_rate
        name: avail
        metric: err_total
        denominator_metric: req_total
        objective: "99.9"
        min_events: 20
"""),
    "010": ("010", "    mysql_connections: 60\n"),
}

# Source text that must survive every rewrite (file → lines).
_KEPT_LINES = {
    "t-oct": ["mysql_connections: 010"],
    "t-clock": ["mysql_connections: 12:30"],
    "t-hex": ["mysql_connections: 0x1F"],
    "t-yes": ["_severity_dedup: yes"],
    "t-quoted": ["mysql_connections: '010'"],
    "t-typed": ["expires: 2099-01-01T00:00:00Z", "tier: 1", "min_events: 20",
                "send_resolved: false"],
    "010": ["  010:"],
}


def _tenant_text(tid: str) -> str:
    key, body = _TENANT_BODIES[tid]
    return f"tenants:\n  {key}:\n{body}    mysql_slave_lag: 20\n"


def _multi_file() -> dict[str, str]:
    files = {"_defaults.yaml": _DEFAULTS}
    files.update({f"{tid}.yaml": _tenant_text(tid) for tid in _TENANT_BODIES})
    return files


def _legacy() -> dict[str, str]:
    tenants = "".join(_tenant_text(tid)[len("tenants:\n"):]
                      for tid in _TENANT_BODIES)
    return {"config.yaml": _DEFAULTS + "tenants:\n" + tenants}


def _write_tree(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    return root


def _read_tree(root: Path) -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(root.iterdir())}


# ── the oracle ───────────────────────────────────────────────────────────

def test_go_present_when_required():
    """Fail-closed: in a job that requires go, a missing go would silently
    skip every exporter-oracle test below."""
    if _REQUIRE_GO:
        assert _HAS_GO, "VIBE_REQUIRE_GO=1 but `go` is not on PATH"


@pytest.fixture(scope="module")
def da_guard(tmp_path_factory):
    out = tmp_path_factory.mktemp("da-guard") / "da-guard"
    env = dict(os.environ)
    env.setdefault("GOTOOLCHAIN", "auto")
    subprocess.run(["go", "build", "-buildvcs=false", "-o", str(out),
                    "./cmd/da-guard"], cwd=APP, env=env, check=True,
                   timeout=600)
    return str(out)


def _served(conf_d: Path, da_guard: str) -> dict:
    """{tenant: (values, severities, unserved, dropped)} as /metrics serves
    it. A file the exporter cannot decode raises (`parse_failed`)."""
    got = tv.load_served_values(conf_d, at=AT, binary=da_guard)
    return {t: (v.values, v.severities, v.unserved, v.dropped)
            for t, v in got.items()}


_SECTIONS = ("values", "severities", "unserved", "dropped")


def _changes(expected: dict, got: dict) -> list[str]:
    """Every `tenant.section.key: expected -> got` that differs (readable red)."""
    out = []
    for t in sorted(set(expected) | set(got)):
        if t not in got or t not in expected:
            out.append(f"{t}: {'missing after' if t not in got else 'new'}")
            continue
        for name, a, b in zip(_SECTIONS, expected[t], got[t]):
            for k in sorted(set(a) | set(b)):
                if a.get(k) != b.get(k):
                    out.append(f"{t}.{name}.{k}: {a.get(k)!r} -> {b.get(k)!r}")
    return out


def _minus(served: dict, key: str, tenants=None) -> dict:
    """`served` with `key` gone from every section of `tenants` (all: None)."""
    out = {}
    for t, sections in served.items():
        if tenants is None or t in tenants:
            sections = tuple({k: v for k, v in s.items() if k != key}
                             for s in sections)
        out[t] = sections
    return out


# ── deprecate_rule --execute ─────────────────────────────────────────────

@needs_go
def test_deprecate_rule_changes_only_the_deprecated_key(tmp_path, da_guard):
    d = _write_tree(tmp_path / "conf.d", _multi_file())
    before = _served(d, da_guard)
    p = subprocess.run([sys.executable, str(DEPRECATE), "mysql_slave_lag",
                        "--config-dir", str(d), "--execute"],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    after_text = _read_tree(d)
    # Every file was rewritten (the precondition of this test meaning anything).
    assert all("mysql_slave_lag" not in t for t in after_text.values()), after_text
    assert _changes(_minus(before, "mysql_slave_lag"), _served(d, da_guard)) == []
    for tid, lines in _KEPT_LINES.items():
        for line in lines:
            assert line in after_text[f"{tid}.yaml"], (tid, after_text[f"{tid}.yaml"])
    for line in ("mysql_threads_running: 010", "container_cpu: 80.5",
                 "max_metrics_per_tenant: 500", "send_resolved: true"):
        assert line in after_text["_defaults.yaml"], after_text["_defaults.yaml"]


# ── patch_config (_patch_carrier) ────────────────────────────────────────

def _patched(files: dict[str, str], mode: str, tid: str, key: str,
             value: str) -> dict[str, str]:
    cm = {"metadata": {"resourceVersion": "1"}, "data": dict(files)}
    assert pc.detect_mode(cm) == mode
    patch = pc.build_patch(cm, mode, tid, key, value)
    assert patch is not None
    return {**files, **patch["data"]}


def _as_confd(files: dict[str, str]) -> dict[str, str]:
    """The tree da-guard is pointed at. Multi-file: the keys as they are.
    Legacy: da-guard has no single-file mode, and in a directory the
    exporter ignores a tenant file's `defaults:` — so the ONE written
    `config.yaml` is cut, as text (nothing re-serialised), at its top-level
    `tenants:` line: the part above becomes `_defaults.yaml`, the rest the
    tenant file. Single-file mode reads those same two parts as one config;
    every byte the rewrite produced is decoded by the exporter."""
    if set(files) != {"config.yaml"}:
        return files
    text = files["config.yaml"]
    head, sep, tail = text.partition("\ntenants:\n")
    assert sep and "\ntenants:" not in tail, text
    return {"_defaults.yaml": head + "\n", "config.yaml": "tenants:\n" + tail}


@needs_go
@pytest.mark.parametrize("tid", sorted(_TENANT_BODIES))
@pytest.mark.parametrize("layout", ["multi-file", "legacy"])
def test_patch_config_changes_only_the_patched_key(tmp_path, da_guard,
                                                   layout, tid):
    files = _multi_file() if layout == "multi-file" else _legacy()
    before = _served(_write_tree(tmp_path / "before", _as_confd(files)),
                     da_guard)
    written = _patched(files, layout, tid, "mysql_threads_running", "50")
    after = _served(_write_tree(tmp_path / "after", _as_confd(written)),
                    da_guard)
    assert _changes(_minus(before, "mysql_threads_running", {tid}),
                    _minus(after, "mysql_threads_running", {tid})) == []
    assert after[tid][0]["mysql_threads_running"] == 50.0
    # The rewritten key: what was not patched is written as it was read; the
    # new value is written as before #2220 (`'50'`, quoted: YAML would
    # retype it).
    rewritten = "config.yaml" if layout == "legacy" else f"{tid}.yaml"
    for line in _KEPT_LINES[tid]:
        assert line in written[rewritten], written[rewritten]
    assert "mysql_threads_running: '50'" in written[rewritten], written[rewritten]
    if layout == "legacy":
        for line in ("mysql_threads_running: 010", "max_metrics_per_tenant: 500"):
            assert line in written["config.yaml"], written["config.yaml"]


# ── a file the non-rewrite read refuses is not rewritten either ──────────
#
# `=` resolves to `!!value` and `<<` (as a value) to `!!merge`; SafeConstructor
# has no constructor for either, so deprecate_rule's scan names the file
# unreadable (rc 1) and patch_config's pre-#2220 reader refused it (rc 2). The
# rewrite read must agree — before the #2220 review it read them as RawPlain
# and deprecate_rule deleted the key and rewrote a file its own scan had
# called unreadable. Expected rc / "untouched" are what main (7b992d8b) does.

_UNREADABLE = ["=", "<<"]


def _unreadable_tenant(bad: str) -> str:
    return (f"tenants:\n  t1:\n    mysql_slave_lag: 20\n"
            f"    _metadata: {{owner: {bad}}}\n")


@pytest.mark.parametrize("where", ["tenant", "defaults"])
@pytest.mark.parametrize("bad", _UNREADABLE)
def test_deprecate_rule_does_not_rewrite_a_file_its_scan_cannot_read(
        tmp_path, bad, where):
    files = {"_defaults.yaml": "defaults:\n  mysql_slave_lag: 30\n",
             "t.yaml": _unreadable_tenant(bad)}
    if where == "defaults":
        files["_defaults.yaml"] += f"_metadata: {{owner: {bad}}}\n"
    d = _write_tree(tmp_path / "conf.d", files)
    target = "t.yaml" if where == "tenant" else "_defaults.yaml"
    p = subprocess.run([sys.executable, str(DEPRECATE), "mysql_slave_lag",
                        "--config-dir", str(d), "--execute"],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 1, p.stdout + p.stderr
    assert "無法讀取" in p.stdout + p.stderr, p.stdout + p.stderr
    assert (d / target).read_text(encoding="utf-8") == files[target]


@pytest.mark.parametrize("bad", _UNREADABLE)
@pytest.mark.parametrize("layout", ["multi-file", "legacy"])
def test_patch_config_refuses_a_key_its_reader_cannot_construct(layout, bad):
    """Run in a FRESH interpreter: `generate_crd_schemas` registers a `!!value`
    constructor on `yaml.SafeLoader` at import, so in a pytest worker that
    imported it `=` is constructible for every loader and the refusal cannot
    be observed (measured: this test went green-then-red by test order)."""
    tenant = _unreadable_tenant(bad)
    data = ({"_defaults.yaml": "defaults:\n  m: 1\n", "t.yaml": tenant}
            if layout == "multi-file" else
            {"config.yaml": "defaults:\n  m: 1\n" + tenant})
    cm = {"metadata": {"resourceVersion": "1"}, "data": data}
    script = (
        "import json, sys\n"
        "from unittest import mock\n"
        "import yaml\n"
        "import patch_config as pc\n"
        "cm, layout = json.loads(sys.argv[1]), sys.argv[2]\n"
        "try:\n"
        "    pc.build_patch(cm, layout, 't1', 'mysql_slave_lag', '5')\n"
        "    built = 'built'\n"
        "except yaml.constructor.ConstructorError as e:\n"
        "    built = 'ConstructorError: ' + str(e).splitlines()[0]\n"
        "argv = ['patch_config.py', '--diff', 't1', 'mysql_slave_lag', '5']\n"
        "with mock.patch('patch_config.run_cmd', return_value=json.dumps(cm)) as run, \\\n"
        "        mock.patch('sys.argv', argv):\n"
        "    try:\n"
        "        pc.main(); rc = 0\n"
        "    except SystemExit as e:\n"
        "        rc = e.code\n"
        "sent = [c.args[0] for c in run.call_args_list if 'patch' in c.args[0]]\n"
        "print(json.dumps({'built': built, 'rc': rc, 'sent': sent}))\n")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(REPO / "scripts" / "tools"), str(REPO / "scripts" / "tools" / "ops")])}
    p = subprocess.run([sys.executable, "-c", script, json.dumps(cm), layout],
                       capture_output=True, text=True, timeout=60, env=env)
    got = json.loads(p.stdout.strip().splitlines()[-1])
    assert got["built"].startswith(
        "ConstructorError: could not determine a constructor"), (got, p.stderr)
    assert got["rc"] == 2 and got["sent"] == [], got


def test_patch_config_writes_a_new_severity_value_plain():
    """`70:critical` is a string to YAML either way: written plain, as before."""
    written = _patched(_multi_file(), "multi-file", "t-clock",
                       "mysql_connections", "70:critical")
    assert "mysql_connections: 70:critical\n" in written["t-clock.yaml"]
    assert "mysql_slave_lag: 20\n" in written["t-clock.yaml"]


# ── the loader / dumper pair itself (no Go) ──────────────────────────────

# (source spelling, how the rewrite writes it). Plain → as written; quoted →
# `safe_dump`'s own quoting of that string, as before #2220; null → `null`,
# as before.
_SPELLINGS = [("010", "010"), ("12:30", "12:30"), ("0x1F", "0x1F"),
              ("yes", "yes"), ("2099-01-01T00:00:00Z", "2099-01-01T00:00:00Z"),
              ("80.5", "80.5"), ("500", "500"), ("true", "true"),
              ("plain words", "plain words"), ("1_000", "1_000"),
              ("0o17", "0o17"), (".inf", ".inf"), ("70:critical", "70:critical"),
              ("'010'", "'010'"), ('"12:30"', "'12:30'"), ('"a b"', "a b")]


@pytest.mark.parametrize("load", [load_for_rewrite, strict_load_for_rewrite],
                         ids=["load_for_rewrite", "strict_load_for_rewrite"])
@pytest.mark.parametrize("spelling,written", _SPELLINGS,
                         ids=[s for s, _w in _SPELLINGS])
def test_a_value_and_a_key_round_trip_as_written(load, spelling, written):
    got = dump_for_rewrite(load(f"k: {spelling}\n{spelling}: v\n"),
                           sort_keys=False)
    assert got == f"k: {written}\n{written}: v\n"


@pytest.mark.parametrize("spelling", ["~", "", "null"])
def test_a_null_value_is_written_null_as_before(spelling):
    assert dump_for_rewrite(load_for_rewrite(f"k: {spelling}\n")) == "k: null\n"


def test_null_stays_none_and_quoted_stays_a_plain_str():
    doc = load_for_rewrite("a: ~\nb: '010'\nc: 010\n")
    assert doc["a"] is None
    assert type(doc["b"]) is str and doc["b"] == "010"
    assert isinstance(doc["c"], RawPlain) and doc["c"] == "010"


def test_a_new_str_value_is_quoted_when_yaml_would_retype_it():
    doc = load_for_rewrite("t:\n  a: 1\n")
    doc["t"]["b"] = "50"
    doc["t"]["c"] = "70:critical"
    assert dump_for_rewrite(doc, sort_keys=False) == \
        "t:\n  a: 1\n  b: '50'\n  c: 70:critical\n"


def test_an_explicit_tag_is_not_taken_for_plain():
    """`!!str 5` is a string the exporter's float field refuses; written back
    plain `5` it would become a number — a change, so it is not RawPlain."""
    doc = load_for_rewrite("a: !!str 5\n")
    assert type(doc["a"]) is str
    assert dump_for_rewrite(doc) == "a: '5'\n"


def test_the_file_entry_point_keeps_the_load_contract(tmp_path):
    assert load_yaml_file_for_rewrite(str(tmp_path / "nope.yaml"), {}) == {}
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert load_yaml_file_for_rewrite(str(empty), {}) == {}
    bad = tmp_path / "bad.yaml"
    bad.write_bytes(b"a: [\n")
    with pytest.raises(yaml.YAMLError, match="bad.yaml"):
        load_yaml_file_for_rewrite(str(bad))


def test_the_strict_entry_point_still_rejects_a_duplicate_key():
    with pytest.raises(DuplicateKeyError):
        strict_load_for_rewrite("010: a\n'010': b\n")


def _load_via_file(tmp_path: Path, text: str):
    p = tmp_path / "f.yaml"
    p.write_text(text, encoding="utf-8")
    return load_yaml_file_for_rewrite(str(p))


@pytest.mark.parametrize("entry", ["load_for_rewrite", "strict_load_for_rewrite",
                                   "load_yaml_file_for_rewrite"])
def test_the_rewrite_loaders_cannot_construct_python_objects(entry, tmp_path):
    """Behavioural safety pin (see `_lib_yaml_keys.load_exporter_keys`): the
    rewrite loaders are hand-driven SafeLoader subclasses, outside bandit
    B506's predicate — this is what pins them. Must-still-work control."""
    load = {"load_for_rewrite": load_for_rewrite,
            "strict_load_for_rewrite": strict_load_for_rewrite,
            "load_yaml_file_for_rewrite":
                lambda text: _load_via_file(tmp_path, text)}[entry]
    payload = '!!python/object/apply:os.system ["echo rewrite-loader-pwned"]\n'
    with pytest.raises(yaml.YAMLError) as exc:
        load(payload)
    cause = getattr(exc.value, "cause", exc.value)
    assert isinstance(cause, yaml.constructor.ConstructorError), exc.value
    assert load("a: [1, 2]\n") == {"a": ["1", "2"]}


def test_dump_for_rewrite_is_safe_dump_for_ordinary_data():
    """No RawPlain in the data → byte-identical to `yaml.safe_dump`."""
    data = {"a": 8, "b": "010", "c": [True, None, 1.5], "d": {"e": "x y"}}
    assert dump_for_rewrite(data, default_flow_style=False, allow_unicode=True,
                            sort_keys=False) == yaml.safe_dump(
        data, default_flow_style=False, allow_unicode=True, sort_keys=False)


def test_deprecate_rule_still_names_a_scalar_defaults_by_its_yaml_type(tmp_path):
    """The refusal names the type YAML gives it (`int`), not `RawPlain`."""
    (tmp_path / "_defaults.yaml").write_text("defaults: 5\n", encoding="utf-8")
    [(_p, ok, msg, _r)] = deprecate_rule.remove_from_all_defaults(
        "mysql_slave_lag", str(tmp_path))
    assert not ok and "（int）" in msg, msg


def test_a_reader_that_is_not_a_rewrite_still_gets_pyyaml_types():
    """Rewrite paths only: the ordinary exporter-key read is unchanged."""
    from _lib_yaml_keys import load_exporter_keys
    assert load_exporter_keys(io.StringIO("a: 010\nb: yes\n")) == {"a": 8, "b": True}
