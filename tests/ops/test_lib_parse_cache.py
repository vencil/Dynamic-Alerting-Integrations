"""#2901（ADR-036 §3）：conf.d 解析快取 `_lib_parse_cache`。

釘住的契約：

1. `generate-routes --validate --strict` 路徑上每一處把 conf.d 檔解析成物件的
   呼叫都經 `read_confd_yaml`（以 AST 窮舉呼叫點，並以實跑記錄 YAML reader
   的建立位置對照）。
2. key 的 blob id 與 `git hash-object` 相同（sha1 與 sha256 兩種 repo）。
3. 沒裝快取時讀法不變（逐位元組的證明在 `test_grar_findings.py` 的 golden）；
   裝了快取時，同一棵樹的 rc 與 findings 和不裝時相同，冷、熱快取皆然。
4. 讀回快取用限制型 unpickler：只放行解析器實際產生的型別，其他一律當 miss。
5. 解析失敗不進快取；MemoryError 記在快取上。
6. 只改一個檔時，實際呼叫解析器的次數＝那個檔的解析模式數。

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import ast
import contextlib
import datetime
import io
import os
import pickle
import subprocess
import sys
import traceback
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _grar_findings_cases as cases  # noqa: E402

import _grar_parse  # noqa: E402
import _lib_confd  # noqa: E402
import _lib_parse_cache as pc  # noqa: E402
import generate_alertmanager_routes as gar  # noqa: E402

REPO = cases.REPO
TOOLS = REPO / "scripts" / "tools"

# Every ParseMode the generator's conf.d read path uses (module, name).
MODES = {
    ("_grar_parse", "ROUTING_READ"),
    ("_grar_parse", "NULL_KEY_PROBE"),
    ("_lib_confd", "STRICT_KEYS_READ"),
    ("_lib_confd", "FIRST_DOCUMENT_KEYS_READ"),
}
# The modules the generator imports that may parse conf.d content.
READER_MODULES = [TOOLS / "_lib_confd.py", *sorted((TOOLS / "ops").glob("_grar_*.py")),
                  TOOLS / "ops" / "generate_alertmanager_routes.py"]
# YAML entry points; a call to one of these in a reader module must be a
# parse function handed to ParseMode, or read a file that is not conf.d.
_PARSER_NAMES = {"safe_load", "safe_load_all", "load", "load_all",
                 "strict_load_exporter_keys", "strict_load_all_exporter_keys",
                 "strict_safe_load", "load_exporter_keys",
                 "load_first_document_exporter_keys", "load_all_exporter_keys",
                 "get_single_data", "get_data"}
# (file, enclosing function) of the parser calls that read something other
# than a conf.d file — each one named with what it reads.
NOT_CONFD = {
    # the generated Alertmanager config / ConfigMap (this run's own output)
    ("_grar_render.py", "evaluate_generated_config"),
    ("_grar_render.py", "apply_to_configmap"),
    ("generate_alertmanager_routes.py", "_output_configmap_mode"),
    # --apply: the ConfigMap read back from the cluster (kubectl)
    ("_grar_render.py", "_read_existing_configmap"),
    # --base-config / --policy: operator files outside conf.d (ADR-036 §3:
    # the deployment policy is read fresh for every judgement)
    ("_grar_render.py", "load_base_config"),
    ("_grar_validate.py", "load_policy_document"),
    # the platform rules pack shipped beside the tool
    ("_grar_validate.py", "platform_alert_identities"),
}
# The parse functions handed to ParseMode.
PARSE_FUNCTIONS = {
    ("_grar_parse.py", "<lambda>"),
    ("_grar_parse.py", "_null_key_probe_parse"),
    ("_lib_confd.py", "_first_document_keys"),
    ("_lib_confd.py", "_strict_keys"),
}


def _parser_calls(path: Path) -> set[tuple[str, str]]:
    """(file, enclosing function) of every call to a YAML entry point."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[tuple[str, str]] = set()

    def walk(node, func):
        for child in ast.iter_child_nodes(node):
            name = func
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            elif isinstance(child, ast.Lambda):
                name = "<lambda>"
            if isinstance(child, ast.Call):
                f = child.func
                callee = f.attr if isinstance(f, ast.Attribute) else (
                    f.id if isinstance(f, ast.Name) else None)
                if callee in _PARSER_NAMES:
                    found.add((path.name, func))
            walk(child, name)

    walk(tree, "<module>")
    return found


def test_every_parser_call_in_the_reader_modules_is_accounted_for():
    """AST: a YAML parse in the generator's modules is either a ParseMode's
    parse function (so it runs only through read_confd_yaml) or reads a
    named non-conf.d file. A new parse of a conf.d file outside the cache
    turns this red."""
    calls = set().union(*(_parser_calls(p) for p in READER_MODULES))
    assert calls - NOT_CONFD - PARSE_FUNCTIONS == set()
    assert PARSE_FUNCTIONS <= calls  # the list is not stale
    stale = NOT_CONFD - calls
    assert not stale, stale


def test_the_modes_are_the_named_ones_and_distinct():
    found = set()
    for modname in ("_grar_parse", "_lib_confd"):
        mod = sys.modules[modname]
        for name, value in vars(mod).items():
            if isinstance(value, pc.ParseMode):
                found.add((modname, name))
    assert found == MODES
    names = [getattr(sys.modules[m], n).name for m, n in MODES]
    assert len(set(names)) == len(names)
    # ROUTING_READ names its argument: the same parser without it is a
    # different mode (_lib_confd.STRICT_KEYS_READ).
    assert "raw_text_sequences=('tenants',)" in _grar_parse.ROUTING_READ.name


def _record_yaml_readers(run):
    """For every yaml Reader built while *run* runs: the stream's name and
    whether ``_lib_parse_cache._parse_file`` is on the stack."""
    sites = []
    orig = yaml.reader.Reader.__init__

    def rec(self, stream):
        through = any(f.name == "_parse_file"
                      and f.filename.endswith("_lib_parse_cache.py")
                      for f in traceback.extract_stack())
        sites.append((getattr(stream, "name", None), through))
        return orig(self, stream)

    yaml.reader.Reader.__init__ = rec
    try:
        run()
    finally:
        yaml.reader.Reader.__init__ = orig
    return sites


def _quiet_main(argv):
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        try:
            gar.main(argv)
        except SystemExit as exc:
            return gar._exit_code_of(exc)
    return 0


# A tree that reaches every conf.d read: a root platform `tenants:` overlay
# (declared_tenant_ids), an unselected carrier spelling below the root
# (_enforced_in_unread_file), a route `match` (the null-key probe).
_EVERY_READ = {
    "_defaults.yaml": cases.FIXTURES["clean"]["_defaults.yaml"]
    + "tenants:\n  alpha:\n    mysql_connections: 60\n",
    "alpha.yaml": cases._tenant("alpha", extra=(
        "      routes:\n        - match: {severity: critical}\n"
        "          receiver: {type: webhook, url: https://hooks.example.com/x}\n")),
    "team/_defaults.yaml": "defaults: {}\n",
    "team/_defaults.yml": "defaults: {}\n",
    "team/beta.yaml": cases._tenant("beta"),
}


def _write_tree(root: Path, files: dict[str, str]) -> Path:
    d = root / "conf.d"
    for rel, body in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return d


def test_runtime_every_confd_parse_goes_through_the_cache(tmp_path, monkeypatch):
    """Run the generator over a tree that reaches every read: every YAML
    reader built for a conf.d file is built inside read_confd_yaml, and
    every mode is used."""
    monkeypatch.setenv("PATH", os.devnull)
    d = _write_tree(tmp_path, _EVERY_READ)
    _lib_confd.reset_warned_for_test()
    seen_modes = set()
    orig = pc._parse_file

    def spy(path, mode):
        seen_modes.add(mode.name)
        return orig(path, mode)

    monkeypatch.setattr(pc, "_parse_file", spy)
    sites = _record_yaml_readers(lambda: _quiet_main(
        ["--config-dir", str(d), "--validate", "--strict"]))
    confd = [through for name, through in sites
             if isinstance(name, str) and name.startswith(str(d))]
    assert confd and all(confd), sites
    assert seen_modes == {getattr(sys.modules[a], b).name for a, b in MODES}


def _git(cwd, *args, stdin=None):
    return subprocess.run(["git", *args], cwd=cwd, input=stdin,
                          capture_output=True, check=True, timeout=120).stdout


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
def test_blob_id_matches_git_hash_object(tmp_path, fmt):
    _git(tmp_path, "init", "-q", f"--object-format={fmt}")
    for data in (b"", b"tenants: {}\n", "租戶: x\n".encode("utf-8"),
                 bytes(range(256)) * 40):
        want = _git(tmp_path, "hash-object", "--stdin", stdin=data).decode().strip()
        assert pc.git_blob_id(data, fmt) == want


def test_unknown_object_format_is_refused():
    with pytest.raises(ValueError):
        pc.git_blob_id(b"", "md5")
    with pytest.raises(ValueError):
        pc.ParseCache({}, "md5")


# Every tag PyYAML's SafeConstructor builds, plus what the exporter-key
# loaders add (keys as text) — the universe of values a cache entry holds.
_EVERY_TYPE = """\
tenants:
  alpha: {a: 1, b: 1.5, c: .inf, d: .nan, e: true, f: ~, g: text, h: 0x1f}
  bin: !!binary aGVsbG8=
  date: 2024-01-02
  ts: 2024-01-02T03:04:05Z
  tsoff: 2024-01-02 03:04:05.5 +08:00
  naive: 2024-01-02 03:04:05
  set: !!set {x, y}
  omap: !!omap [{p: 1}, {q: 2}]
  pairs: !!pairs [{p: 1}, {p: 2}]
  seq: [1, [2, [3]]]
  big: 123456789012345678901234567890
  anchor: &x {k: v}
  alias: *x
"""


class _Recording(pickle.Unpickler):
    seen: set = set()

    def find_class(self, module, name):
        _Recording.seen.add((module, name))
        return super().find_class(module, name)


@pytest.mark.parametrize("mode", sorted(MODES))
def test_allowed_globals_are_exactly_what_the_parsers_produce(tmp_path, mode):
    """Round-trip a value of every type through each mode: the globals the
    pickle names are within ALLOWED_GLOBALS, the restricted read gives an
    equal value, and every allowed global is needed by some value."""
    f = tmp_path / "t.yaml"
    f.write_text(_EVERY_TYPE, encoding="utf-8")
    pm = getattr(sys.modules[mode[0]], mode[1])
    with open(f, encoding="utf-8") as fh:
        value = pm.parse(fh)
    raw = pickle.dumps(value, protocol=pc.PICKLE_PROTOCOL)
    _Recording.seen = set()
    _Recording(io.BytesIO(raw)).load()
    assert _Recording.seen == set(pc.ALLOWED_GLOBALS)
    back = pc.restricted_loads(raw)
    assert repr(back) == repr(value)
    # shared references survive (an alias is one object)
    t = back["tenants"]
    assert t["anchor"] is t["alias"]


class _Evil:
    def __reduce__(self):
        return (os.system, ("true",))


@pytest.mark.parametrize("payload", [
    _Evil(),                         # posix.system
    datetime.date.today,             # builtins.getattr
    Path("/x"),                      # pathlib
    {"k": complex(1, 2)},            # builtins.complex
], ids=["os.system", "getattr", "pathlib", "complex"])
def test_restricted_loads_refuses_other_globals(payload):
    raw = pickle.dumps(payload, protocol=pc.PICKLE_PROTOCOL)
    with pytest.raises(pickle.UnpicklingError):
        pc.restricted_loads(raw)


def test_restricted_loads_control_needs_no_global():
    value = {"s": {1, 2}, "f": frozenset({3}), "t": (1, "x"), "b": b"\x00",
             "d": datetime.datetime(2024, 1, 2, tzinfo=datetime.timezone.utc)}
    assert pc.restricted_loads(pickle.dumps(value, protocol=pc.PICKLE_PROTOCOL)) == value


def test_an_entry_that_does_not_load_is_a_miss_and_is_reparsed(tmp_path):
    f = tmp_path / "t.yaml"
    f.write_text("tenants: {alpha: {}}\n", encoding="utf-8")
    key = (pc.git_blob_id(f.read_bytes()), _grar_parse.ROUTING_READ.name)
    cache = pc.ParseCache({key: pickle.dumps(_Evil())})
    assert cache.load(f, _grar_parse.ROUTING_READ) == {"tenants": {"alpha": {}}}
    assert (cache.parses, cache.rejected_entries, len(cache.hits)) == (1, 1, 0)
    assert key in cache.new  # the re-parse replaces it


def test_a_hit_is_a_fresh_copy(tmp_path):
    """The generator may change what it was handed; the next read of the
    same content must not see that."""
    f = tmp_path / "t.yaml"
    f.write_text("tenants: {alpha: {x: 1}}\n", encoding="utf-8")
    cache = pc.ParseCache()
    first = cache.load(f, _grar_parse.ROUTING_READ)
    first["tenants"]["alpha"]["x"] = 2
    again = cache.load(f, _grar_parse.ROUTING_READ)
    assert again == {"tenants": {"alpha": {"x": 1}}}
    assert (cache.parses, len(cache.hits)) == (1, 1)


def test_a_failed_parse_is_not_cached(tmp_path):
    f = tmp_path / "t.yaml"
    f.write_text("a: 1\na: 2\n", encoding="utf-8")  # duplicate key (#2123)
    cache = pc.ParseCache()
    for _ in range(2):
        with pytest.raises(yaml.YAMLError):
            cache.load(f, _grar_parse.ROUTING_READ)
    assert cache.new == {} and cache.parses == 2


def test_a_memory_error_is_recorded_and_raised(tmp_path):
    f = tmp_path / "t.yaml"
    f.write_text("x: 1\n", encoding="utf-8")

    def boom(_stream):
        raise MemoryError

    cache = pc.ParseCache()
    with pytest.raises(MemoryError):
        cache.load(f, pc.ParseMode("boom", boom))
    assert cache.memory_error and cache.new == {}


def test_an_unreadable_file_raises_what_the_uncached_read_raises(tmp_path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(FileNotFoundError) as uncached:
        pc.read_confd_yaml(missing, _grar_parse.ROUTING_READ)
    cache = pc.ParseCache()
    with pytest.raises(FileNotFoundError) as cached:
        cache.load(missing, _grar_parse.ROUTING_READ)
    assert str(cached.value) == str(uncached.value)


def _run_findings(d: Path, out: Path):
    rc = _quiet_main(["--config-dir", str(d), "--validate", "--strict",
                      "--findings-json", str(out)])
    import json
    doc = json.loads(out.read_text(encoding="utf-8"))
    return rc, doc["findings"]


@pytest.mark.parametrize("fixture", sorted(cases.FIXTURES) + ["every_read"])
def test_cached_and_uncached_runs_agree(tmp_path, monkeypatch, fixture):
    """Same tree, no cache vs a cold cache vs a warm one: same rc, same
    findings."""
    monkeypatch.setenv("PATH", os.devnull)
    files = _EVERY_READ if fixture == "every_read" else cases.FIXTURES[fixture]
    d = _write_tree(tmp_path, files)
    results = []
    entries: dict = {}
    failed_parses = None
    for label in ("none", "cold", "warm"):
        _lib_confd.reset_warned_for_test()
        cache = None if label == "none" else pc.ParseCache(dict(entries))
        pc.install(cache)
        try:
            results.append(_run_findings(d, tmp_path / f"{label}.json"))
        finally:
            pc.install(None)
        if label == "cold":
            entries.update(cache.new)
            failed_parses = cache.parses - len(cache.new)
        elif label == "warm":
            # only the parses that raised (never cached) run again
            assert cache.new == {} and cache.parses == failed_parses
    assert results[0] == results[1] == results[2]


def test_changing_one_file_parses_only_that_file(tmp_path, monkeypatch):
    """Second judgement of the same tree with one file changed: the parser
    runs once per mode that file is read in, and for nothing else."""
    monkeypatch.setenv("PATH", os.devnull)
    d = _write_tree(tmp_path, _EVERY_READ)
    _lib_confd.reset_warned_for_test()
    cold = pc.ParseCache()
    pc.install(cold)
    try:
        _quiet_main(["--config-dir", str(d), "--validate", "--strict"])
    finally:
        pc.install(None)
    for rel in ("alpha.yaml", "_defaults.yaml", "team/beta.yaml"):
        f = d / rel
        old_blob = pc.git_blob_id(f.read_bytes())
        modes_of_file = {m for (b, m) in cold.new if b == old_blob}
        assert modes_of_file, rel
        f.write_text(f.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
        new_blob = pc.git_blob_id(f.read_bytes())
        warm = pc.ParseCache(dict(cold.new))
        _lib_confd.reset_warned_for_test()
        pc.install(warm)
        try:
            _quiet_main(["--config-dir", str(d), "--validate", "--strict"])
        finally:
            pc.install(None)
        assert warm.parses == len(modes_of_file), rel
        assert {b for (b, _m) in warm.new} == {new_blob}, rel
        cold.new.update(warm.new)


def test_each_new_entry_is_handed_on_as_it_is_made(tmp_path):
    """#2901 E1: on_store sees every stored entry the moment it exists —
    before the next parse starts — and never a failed parse."""
    a, b, bad = tmp_path / "a.yaml", tmp_path / "b.yaml", tmp_path / "bad.yaml"
    a.write_text("tenants: {alpha: {}}\n", encoding="utf-8")
    b.write_text("tenants: {beta: {}}\n", encoding="utf-8")
    bad.write_text("x: 1\nx: 2\n", encoding="utf-8")
    seen = []
    cache = pc.ParseCache(on_store=lambda key, raw: seen.append((key, raw)))
    cache.load(a, _grar_parse.ROUTING_READ)
    assert len(seen) == 1 and seen[0] == next(iter(cache.new.items()))
    with pytest.raises(yaml.YAMLError):
        cache.load(bad, _grar_parse.ROUTING_READ)
    cache.load(a, _grar_parse.ROUTING_READ)  # a hit: nothing new
    cache.load(b, _grar_parse.ROUTING_READ)
    assert [k for k, _ in seen] == list(cache.new) and len(seen) == 2
    assert pc.restricted_loads(seen[1][1]) == {"tenants": {"beta": {}}}
