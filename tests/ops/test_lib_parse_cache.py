"""#2901（ADR-036 §3）→ 步驟 4（#2906）：conf.d 讀取端 `_lib_parse_cache`。

產生器改讀 `da-guard parse-tree`：`ConfdReader` 依路徑讀一棵樹的檔，產生器容器裝的
`ParseCache` 依「路徑＋內容」快取轉換後的值。釘住的契約：

1. 產生器的模組裡沒有任何 PyYAML 呼叫讀 conf.d（AST 窮舉；讀非 conf.d 的呼叫逐一
   列名），實跑時也沒有為 conf.d 檔建任何 PyYAML reader。
2. key 是「路徑＋內容」的 SHA-256 加轉換模式，不論 repo 的 object format（SHA-1
   blob id 可被碰撞）；同一內容兩種模式各自快取、結果不同；同一內容兩個路徑各自
   一筆。
3. 裝了快取時，同一棵樹的 rc 與 findings 和不裝時相同，冷、熱快取皆然。
4. 讀回快取用限制型 unpickler：只放行轉換實際產生的型別（`Timestamp`），其他一律
   當 miss。
5. 讀不了的檔不進快取；MemoryError 記在快取上。
6. 只改一個檔時，只有那個檔走 parse-tree（單檔模式），次數＝它的轉換模式數。

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import pickle
import subprocess
import sys
from pathlib import Path

import pytest

#: The route generator's reader: a judge's (`judging_run`, #3206) — it reads
#: trees Go names paths on and refuses them as its own finding.
pytestmark = pytest.mark.usefixtures("judging_run")
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _grar_findings_cases as cases  # noqa: E402

import _grar_parse  # noqa: E402
import _lib_confd  # noqa: E402
import _lib_parse_cache as pc  # noqa: E402
import _lib_parse_tree as pt  # noqa: E402
import generate_alertmanager_routes as gar  # noqa: E402

REPO = cases.REPO
TOOLS = REPO / "scripts" / "tools"

# The modules the generator imports that may read conf.d content.
READER_MODULES = [TOOLS / "_lib_confd.py", TOOLS / "_lib_parse_cache.py",
                  *sorted((TOOLS / "ops").glob("_grar_*.py")),
                  TOOLS / "ops" / "generate_alertmanager_routes.py"]
# PyYAML entry points (and the repo's PyYAML loaders).
_PARSER_NAMES = {"safe_load", "safe_load_all", "load", "load_all", "compose", "compose_all",
                 "strict_load_exporter_keys", "strict_safe_load", "strict_safe_load_all",
                 "load_exporter_keys", "load_yaml_file", "load_yaml_file_strict",
                 "load_yaml_file_exporter_keys",
                 "load_first_document_exporter_keys", "load_all_exporter_keys",
                 "get_single_data", "get_data"}
_YAML_ONLY = {"safe_load", "safe_load_all", "load", "load_all", "compose", "compose_all"}
# (file, enclosing function) of the PyYAML calls that read something other
# than a conf.d file — each one named with what it reads.
NOT_CONFD = {
    # (#3206: the generated ConfigMap, the cluster's, and --base-config are
    # read by da-crdecode / da-read now — no PyYAML call left for them.)
    # --policy: an operator file outside conf.d (ADR-036 §3: the deployment
    # policy is read fresh for every judgement)
    ("_grar_validate.py", "load_policy_document"),
    # the platform rules pack shipped beside the tool
    ("_grar_validate.py", "platform_alert_identities"),
}


def _parser_calls(path: Path) -> set[tuple[str, str]]:
    """(file, enclosing function) of every call to a PyYAML entry point."""
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
                # The bare PyYAML names only as `yaml.<name>` (`pickle.load`,
                # `_lib_parse_tree.load` are no YAML parse).
                on_yaml = (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                           and f.value.id == "yaml")
                if callee in _PARSER_NAMES and (callee not in _YAML_ONLY or on_yaml):
                    found.add((path.name, func))
            walk(child, name)

    walk(tree, "<module>")
    return found


def test_no_pyyaml_call_in_the_reader_modules_reads_confd():
    """AST: a PyYAML parse in the generator's modules reads a named non-conf.d
    file. A new PyYAML read of a conf.d file turns this red."""
    calls = set().union(*(_parser_calls(p) for p in READER_MODULES))
    assert calls - NOT_CONFD == set()
    stale = NOT_CONFD - calls
    assert not stale, stale


def test_the_modes_are_the_named_ones_and_distinct():
    found = {name for name, value in vars(pc).items() if isinstance(value, pc.ParseMode)}
    assert found == {"VALUES", "TENANT_LISTS_AS_TEXT"}
    assert pc.VALUES.name != pc.TENANT_LISTS_AS_TEXT.name
    assert _grar_parse.ROUTING_READ is pc.TENANT_LISTS_AS_TEXT
    assert "text_sequences=('tenants',)" in pc.TENANT_LISTS_AS_TEXT.name


def _quiet_main(argv):
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        try:
            gar.main(argv)
        except SystemExit as exc:
            return gar._exit_code_of(exc)
    return 0


# A tree that reaches every conf.d read: a root platform `tenants:` overlay
# (declared_tenants), an unselected carrier spelling below the root, a
# route `match`, a domain policy's `tenants:` list.
_EVERY_READ = {
    "_defaults.yaml": cases.FIXTURES["clean"]["_defaults.yaml"]
    + "tenants:\n  alpha:\n    mysql_connections: 60\n",
    "alpha.yaml": cases._tenant("alpha", extra=(
        "      routes:\n        - match: {severity: critical}\n"
        "          receiver: {type: webhook, url: https://hooks.example.com/x}\n")),
    "_domain_policy.yaml": ("domain_policies:\n  fin:\n    tenants: [alpha]\n"
                            "    constraints:\n      forbidden_receiver_types: [email]\n"),
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


def test_runtime_no_pyyaml_reader_is_built_for_a_confd_file(tmp_path, monkeypatch):
    """Run the generator over a tree that reaches every read: no PyYAML
    reader is built over a conf.d file's text, and parse-tree is run."""
    monkeypatch.setenv("PATH", os.devnull)
    d = _write_tree(tmp_path, _EVERY_READ)
    texts = {p.read_text(encoding="utf-8") for p in d.rglob("*.y*ml")}
    built = []
    orig = yaml.reader.Reader.__init__

    def rec(self, stream):
        built.append(stream if isinstance(stream, str) else getattr(stream, "name", stream))
        return orig(self, stream)

    runs = []
    orig_load = pt.load

    def spy(*a, **k):
        runs.append(a[0])
        return orig_load(*a, **k)

    monkeypatch.setattr(yaml.reader.Reader, "__init__", rec)
    monkeypatch.setattr(pt, "load", spy)
    _lib_confd.reset_warned_for_test()
    _quiet_main(["--config-dir", str(d), "--validate", "--strict"])
    assert runs == [d]  # one tree-mode document for the whole run
    confd = [s for s in built if (isinstance(s, str) and (s in texts or s.startswith(str(d))))]
    assert confd == [], confd


def _git(cwd, *args, stdin=None):
    return subprocess.run(["git", *args], cwd=cwd, input=stdin,
                          capture_output=True, check=True, timeout=120).stdout


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
def test_the_key_is_path_and_content_sha256_in_either_object_format(tmp_path, fmt):
    """Not the git blob id: a SHA-1 blob id can be collided, and a shared key
    would serve one content's parse for another."""
    _git(tmp_path, "init", "-q", f"--object-format={fmt}")
    d = tmp_path / "conf.d"
    d.mkdir()
    f = d / "t.yaml"
    for data in (b"", b"tenants: {}\n", "租戶: x\n".encode("utf-8")):
        f.write_bytes(data)
        cache = pc.ParseCache()
        pc.install(cache)
        try:
            pc.ConfdReader(d).read(f)
        finally:
            pc.install(None)
        blob = _git(tmp_path, "hash-object", "--stdin", stdin=data).decode().strip()
        want = hashlib.sha256(len(b"t.yaml").to_bytes(8, "big") + b"t.yaml" + data).hexdigest()
        assert [k[0] for k in cache.new] == [want] and want != blob


def test_one_content_two_paths_two_entries(tmp_path):
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "a.yaml").write_text("tenants: {alpha: {}}\n", encoding="utf-8")
    (d / "b.yaml").write_text("tenants: {alpha: {}}\n", encoding="utf-8")
    cache = pc.ParseCache()
    pc.install(cache)
    try:
        r = pc.ConfdReader(d)
        assert r.read(d / "a.yaml") == r.read(d / "b.yaml")
    finally:
        pc.install(None)
    assert len(cache.new) == 2 and cache.parses == 2


def test_one_content_two_modes_two_entries_two_results(tmp_path):
    """The mode is half the key: the routing read keeps a policy's tenants
    as text, VALUES by tag — both cached, neither served for the other."""
    d = tmp_path / "conf.d"
    d.mkdir()
    f = d / "_domain_policy.yaml"
    f.write_text("domain_policies:\n  fin:\n    tenants: [010]\n", encoding="utf-8")
    cache = pc.ParseCache()
    pc.install(cache)
    try:
        plain = pc.ConfdReader(d).read(f, pc.VALUES)
        text = pc.ConfdReader(d).read(f, _grar_parse.ROUTING_READ)
    finally:
        pc.install(None)
    assert plain["domain_policies"]["fin"]["tenants"] == [8]
    assert text["domain_policies"]["fin"]["tenants"] == ["010"]
    assert cache.parses == 2 and len(cache.new) == 2
    warm = pc.ParseCache(dict(cache.new))
    pc.install(warm)
    try:
        assert pc.ConfdReader(d).read(f, pc.VALUES) == plain
        assert pc.ConfdReader(d).read(f, _grar_parse.ROUTING_READ) == text
    finally:
        pc.install(None)
    assert warm.parses == 0


# Every scalar tag parse-tree reads, nested — the universe of values a cache
# entry holds.
_EVERY_TYPE = """\
tenants:
  alpha: {a: 1, b: 1.5, c: .inf, d: .nan, e: true, f: ~, g: text, h: 0x1f}
  date: 2024-01-02
  ts: 2024-01-02T03:04:05Z
  seq: [1, [2, [3]]]
  big: 123456789012345678901234567890
  anchor: &x {k: v}
  alias: *x
"""


def _canon(x):
    if isinstance(x, dict):
        return ("dict", sorted((repr(_canon(k)), _canon(v)) for k, v in x.items()))
    if isinstance(x, (list, tuple)):
        return (type(x).__name__, [_canon(v) for v in x])
    if isinstance(x, float) and x != x:
        return ("float", "nan")
    return (type(x).__name__, repr(x))


class _Recording(pickle.Unpickler):
    seen: set = set()

    def find_class(self, module, name):
        _Recording.seen.add((module, name))
        return super().find_class(module, name)


@pytest.mark.parametrize("mode", ["VALUES", "TENANT_LISTS_AS_TEXT"])
def test_allowed_globals_are_exactly_what_the_conversions_produce(tmp_path, mode):
    """Round-trip a value of every type through each mode: the globals the
    pickle names are within ALLOWED_GLOBALS, the restricted read gives an
    equal value, and every allowed global is needed by some value."""
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "t.yaml").write_text(_EVERY_TYPE, encoding="utf-8")
    value = pc.ConfdReader(d).read(d / "t.yaml", getattr(pc, mode))
    raw = pickle.dumps(value, protocol=pc.PICKLE_PROTOCOL)
    _Recording.seen = set()
    _Recording(io.BytesIO(raw)).load()
    assert _Recording.seen == set(pc.ALLOWED_GLOBALS)
    back = pc.restricted_loads(raw)
    assert _canon(back) == _canon(value)
    assert back["tenants"]["date"] == pt.Timestamp("2024-01-02")


class _Evil:
    def __reduce__(self):
        return (os.system, ("true",))


@pytest.mark.parametrize("payload", [
    _Evil(),                         # posix.system
    Path("/x"),                      # pathlib
    {"k": complex(1, 2)},            # builtins.complex
], ids=["os.system", "pathlib", "complex"])
def test_restricted_loads_refuses_other_globals(payload):
    raw = pickle.dumps(payload, protocol=pc.PICKLE_PROTOCOL)
    with pytest.raises(pickle.UnpicklingError):
        pc.restricted_loads(raw)


def _cached_read(d: Path, f: Path, cache: pc.ParseCache, mode=None):
    pc.install(cache)
    try:
        return pc.ConfdReader(d).read(f, mode or _grar_parse.ROUTING_READ)
    finally:
        pc.install(None)


def test_an_entry_that_does_not_load_is_a_miss_and_is_reread(tmp_path):
    d = tmp_path / "conf.d"
    d.mkdir()
    f = d / "t.yaml"
    f.write_text("tenants: {alpha: {}}\n", encoding="utf-8")
    key = (pc.content_key("t.yaml", f.read_bytes()), _grar_parse.ROUTING_READ.name)
    cache = pc.ParseCache({key: pickle.dumps(_Evil())})
    assert _cached_read(d, f, cache) == {"tenants": {"alpha": {}}}
    assert (cache.parses, cache.rejected_entries, len(cache.hits)) == (1, 1, 0)
    assert key in cache.new  # the re-read replaces it


def test_a_hit_is_a_fresh_copy(tmp_path):
    """The generator may change what it was handed; the next read of the
    same content must not see that."""
    d = tmp_path / "conf.d"
    d.mkdir()
    f = d / "t.yaml"
    f.write_text("tenants: {alpha: {x: 1}}\n", encoding="utf-8")
    cache = pc.ParseCache()
    first = _cached_read(d, f, cache)
    first["tenants"]["alpha"]["x"] = 2
    again = _cached_read(d, f, cache)
    assert again == {"tenants": {"alpha": {"x": 1}}}
    assert (cache.parses, len(cache.hits)) == (1, 1)


def test_an_unreadable_file_is_not_cached(tmp_path):
    d = tmp_path / "conf.d"
    d.mkdir()
    f = d / "t.yaml"
    f.write_text("a: 1\na: 2\n", encoding="utf-8")  # duplicate key
    cache = pc.ParseCache()
    for _ in range(2):
        with pytest.raises(pt.FileUnreadable) as e:
            _cached_read(d, f, cache)
        assert e.value.kind == "duplicate_key"
    assert cache.new == {} and cache.parses == 2


def test_a_memory_error_is_recorded_and_raised(tmp_path):
    d = tmp_path / "conf.d"
    d.mkdir()
    f = d / "t.yaml"
    f.write_text("x: 1\n", encoding="utf-8")

    def boom(_node):
        raise MemoryError

    cache = pc.ParseCache()
    with pytest.raises(MemoryError):
        _cached_read(d, f, cache, pc.ParseMode("boom", boom))
    assert cache.memory_error and cache.new == {}


def test_a_file_parse_tree_does_not_select_is_not_an_empty_mapping(tmp_path):
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "notes.txt").write_text("a: 1\n", encoding="utf-8")
    with pytest.raises(pc.NotSelected):
        pc.ConfdReader(d).read(d / "notes.txt")


def test_no_da_guard_raises_binary_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("DA_GUARD_BINARY", raising=False)
    monkeypatch.setenv("PATH", os.devnull)
    d = tmp_path / "conf.d"
    d.mkdir()
    with pytest.raises(pt.BinaryMissing):
        pc.ConfdReader(d).require()


def _run_findings(d: Path, out: Path):
    rc = _quiet_main(["--config-dir", str(d), "--validate", "--strict",
                      "--findings-json", str(out)])
    doc = json.loads(out.read_text(encoding="utf-8"))
    return rc, doc["findings"]


@pytest.mark.parametrize("fixture", sorted(cases.FIXTURES) + ["every_read"])
def test_cached_and_uncached_runs_agree(tmp_path, monkeypatch, fixture):
    """Same tree, no cache vs a cold cache vs a warm one: same rc, same
    findings."""
    monkeypatch.setenv("PATH", os.devnull)
    files = {"every_read": _EVERY_READ}.get(fixture) or cases.FIXTURES[fixture]
    d = _write_tree(tmp_path, files)
    results = []
    entries: dict = {}
    failed = None
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
            failed = cache.parses - len(cache.new)
        elif label == "warm":
            # only the reads that raised (never cached) run again
            assert cache.new == {} and cache.parses == failed
    assert results[0] == results[1] == results[2]


def test_changing_one_file_reads_only_that_file(tmp_path, monkeypatch):
    """Second judgement of the same tree with one file changed: parse-tree
    runs in file mode for that file, once per mode it is read in, and runs
    for nothing else (no tree-mode document)."""
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
        old_key = pc.content_key(rel, f.read_bytes())
        modes_of_file = {m for (k, m) in cold.new if k == old_key}
        assert modes_of_file, rel
        f.write_text(f.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
        new_key = pc.content_key(rel, f.read_bytes())
        warm = pc.ParseCache(dict(cold.new))
        trees, files, batches = [], [], []
        orig_load, orig_file, orig_batch = pt.load, pt.load_file, pt.run_files_raw
        monkeypatch.setattr(pt, "load", lambda *a, **k: trees.append(a) or orig_load(*a, **k))
        monkeypatch.setattr(pt, "load_file",
                            lambda *a, **k: files.append(a[1]) or orig_file(*a, **k))
        monkeypatch.setattr(pt, "run_files_raw", lambda root, items, *a, **k: batches.append(
            [r for r, _t in items]) or orig_batch(root, items, *a, **k))
        _lib_confd.reset_warned_for_test()
        pc.install(warm)
        try:
            _quiet_main(["--config-dir", str(d), "--validate", "--strict"])
        finally:
            pc.install(None)
            monkeypatch.setattr(pt, "load", orig_load)
            monkeypatch.setattr(pt, "load_file", orig_file)
            monkeypatch.setattr(pt, "run_files_raw", orig_batch)
        assert warm.parses == len(modes_of_file), rel
        # A file-mode run (alone, or as a one-file batch) for that file only.
        ran = files + [r for b in batches for r in b]
        assert trees == [] and set(ran) == {rel} and len(ran) <= len(modes_of_file), \
            (rel, trees, files, batches)
        assert {k for (k, _m) in warm.new} == {new_key}, rel
        cold.new.update(warm.new)


def test_each_new_entry_is_handed_on_as_it_is_made(tmp_path):
    """#2901 E1: on_store sees every stored entry the moment it exists —
    before the next read starts — and never a failed read."""
    d = tmp_path / "conf.d"
    d.mkdir()
    a, b, bad = d / "a.yaml", d / "b.yaml", d / "bad.yaml"
    a.write_text("tenants: {alpha: {}}\n", encoding="utf-8")
    b.write_text("tenants: {beta: {}}\n", encoding="utf-8")
    bad.write_text("x: 1\nx: 2\n", encoding="utf-8")
    seen = []
    cache = pc.ParseCache(on_store=lambda key, raw: seen.append((key, raw)))
    _cached_read(d, a, cache)
    assert len(seen) == 1 and seen[0] == next(iter(cache.new.items()))
    with pytest.raises(pt.FileUnreadable):
        _cached_read(d, bad, cache)
    _cached_read(d, a, cache)  # a hit: nothing new
    _cached_read(d, b, cache)
    assert [k for k, _ in seen] == list(cache.new) and len(seen) == 2
    assert pc.restricted_loads(seen[1][1]) == {"tenants": {"beta": {}}}


# ── the key is the parse's own bytes (the file changes between read and parse) ──

_OLD = "tenants: {alpha: {}}\n"
_NEW = "tenants: {beta: {}}\n"


def _change_then(orig, f: Path):
    """*orig*, after rewriting *f* — the file changes between the reader's
    read of its bytes and parse-tree's parse."""
    def run(*a, **k):
        f.write_text(_NEW, encoding="utf-8")
        return orig(*a, **k)
    return run


def test_file_mode_stores_the_parse_of_the_bytes_it_keyed(tmp_path, monkeypatch):
    """File mode parses the bytes the key was computed from (fed on stdin):
    the stored value is the old content's, under the old content's key."""
    d = tmp_path / "conf.d"
    d.mkdir()
    f = d / "a.yaml"
    f.write_text(_OLD, encoding="utf-8")
    old_key = (pc.content_key("a.yaml", _OLD.encode()), _grar_parse.ROUTING_READ.name)
    monkeypatch.setattr(pt, "load_file", _change_then(pt.load_file, f))
    cache = pc.ParseCache()
    assert _cached_read(d, f, cache) == {"tenants": {"alpha": {}}}
    assert list(cache.new) == [old_key]
    assert pc.restricted_loads(cache.new[old_key]) == {"tenants": {"alpha": {}}}


def test_every_miss_is_file_mode_on_the_keyed_bytes(tmp_path, monkeypatch):
    """With a cache installed no read is answered from a tree-mode run (which
    would read each file again itself): a cold cache over many files runs
    file mode once per file, and each file changed after its bytes were
    read is stored as the parse of the bytes it was keyed on."""
    d = tmp_path / "conf.d"
    d.mkdir()
    names = [f"t{i:02d}.yaml" for i in range(20)]
    for n in names:
        (d / n).write_text(f"tenants: {{{n[:-5]}: {{}}}}\n", encoding="utf-8")
    trees, files = [], []
    orig_load, orig_file = pt.load, pt.load_file

    def file_mode(root, rel, data, *a, **k):
        files.append(rel)
        (d / rel).write_text(_NEW, encoding="utf-8")  # changed under the read
        return orig_file(root, rel, data, *a, **k)

    monkeypatch.setattr(pt, "load", lambda *a, **k: trees.append(a) or orig_load(*a, **k))
    monkeypatch.setattr(pt, "load_file", file_mode)
    cache = pc.ParseCache()
    pc.install(cache)
    try:
        reader = pc.ConfdReader(d)
        got = [reader.read(d / n, _grar_parse.ROUTING_READ) for n in names]
    finally:
        pc.install(None)
    assert trees == [] and files == names
    assert got == [{"tenants": {n[:-5]: {}}} for n in names]
    for n, want in zip(names, got):
        key = (pc.content_key(n, f"tenants: {{{n[:-5]}: {{}}}}\n".encode()),
               _grar_parse.ROUTING_READ.name)
        assert pc.restricted_loads(cache.new[key]) == want
    assert len(cache.new) == len(names)


# ── the long-running writer's fresh judgement (#3206) ───────────────────

def test_judge_now_judges_afresh_and_leaves_the_memo_alone(tmp_path):
    """`judge_now` / `refusal_now` is the explicit bypass of
    `require_servable`'s once-per-process memo: a servable verdict is not
    kept in `_SERVABLE`, and the next call judges the tree again."""
    key = os.path.realpath(tmp_path)
    before = set(pc._SERVABLE)
    assert pc.refusal_now(tmp_path) == []
    assert key not in pc._SERVABLE and pc._SERVABLE == before
    (tmp_path / "broken.yaml").write_text("a: [\n", encoding="utf-8")
    tree, lines = pc.judge_now(tmp_path)
    assert any("broken.yaml" in ln for ln in lines)
    assert [e.path for e in tree.entries if e.verdict] == ["broken.yaml"]
    assert pc._SERVABLE == before


# ── the cold fill: one batch run, the entries of the per-file fill (#3206) ──

_PROFILE = ("routing_profiles:\n  {name}:\n    receiver:\n      type: webhook\n"
            "      url: https://hooks.example.com/{name}\n")

# Subdirectories at two depths, a carrier at the root and in each directory
# (two spellings in `team/`: one unselected), a domain policy, routing
# profiles at the root and below it, tenants that use them, and files
# parse-tree refuses (a repeated key, a syntax error, a top level that is not
# a mapping, invalid UTF-8) beside ones no reader reads (hidden, not a
# config name).
_COLD_TREE: dict[str, "str | bytes"] = {
    **_EVERY_READ,
    "_routing_profiles.yaml": _PROFILE.format(name="root-p"),
    "team/_routing_profiles.yaml": _PROFILE.format(name="team-p"),
    "team/sub/_defaults.yaml": "defaults:\n  mysql_connections: 75\n",
    "team/sub/bad-dup.yaml": "tenants:\n  dup:\n    x: 1\n    x: 2\n",
    "broken.yaml": "tenants:\n  broken: [unclosed\n",
    "listy.yaml": "- one\n- two\n",
    "not-utf8.yaml": b"tenants:\n  nu:\n    k: \xff\n",
    ".hidden.yaml": "tenants: {hidden: {}}\n",
    "notes.txt": "not a config file\n",
    **{f"team/sub/t{i:02d}.yaml": cases._tenant(f"t{i:02d}", extra=(
        "" if i % 3 else "    _routing_profile: team-p\n"))
       for i in range(12)},
    **{f"other/o{i:02d}.yaml": cases._tenant(f"o{i:02d}") for i in range(8)},
    "other/p.yaml": cases._tenant("p-user") + "    _routing_profile: root-p\n",
}


def _write_cold_tree(root: Path) -> Path:
    d = root / "conf.d"
    for rel, body in _COLD_TREE.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    return d


@pytest.mark.parametrize("entry", ["judgement", "warming"])
def test_cold_fill_in_one_batch_stores_the_per_file_fills_entries(tmp_path, monkeypatch, entry):
    """A cold cache filled through `prefetch` (one `parse-tree --inputs` run
    for the files the generator reads, one for the tenant files'
    declarations) holds byte for byte the entries the per-file fill holds —
    same keys (SHA-256 of path and bytes, mode), same pickled bytes, same
    order — with the same counters, and the run's rc and findings agree;
    file mode runs on its own only for the carrier verdicts of a directory
    with two carriers. The per-file fill is today's miss path with the batch
    refused (`run_files_raw` raising, as an older da-guard would)."""
    monkeypatch.setenv("PATH", os.devnull)
    d = _write_cold_tree(tmp_path)
    orig_file, orig_batch = pt.load_file, pt.run_files_raw

    def fill(batch: bool):
        files, batches = [], []
        if batch:
            monkeypatch.setattr(pt, "run_files_raw", lambda root, items, *a, **k: batches.append(
                [r for r, _t in items]) or orig_batch(root, items, *a, **k))
        else:
            def refused(*_a, **_k):
                raise pt.NotThisDocument("no --inputs", older=True)
            monkeypatch.setattr(pt, "run_files_raw", refused)
        monkeypatch.setattr(pt, "load_file",
                            lambda *a, **k: files.append(a[1]) or orig_file(*a, **k))
        _lib_confd.reset_warned_for_test()
        cache = pc.ParseCache()
        pc.install(cache)
        out = tmp_path / f"findings-{batch}.json"
        try:
            if entry == "judgement":
                result = _run_findings(d, out)
            else:  # the warming run's read (_gsvc_child._warm)
                with contextlib.redirect_stderr(io.StringIO()):
                    try:
                        gar.load_tenant_tree(str(d), strict_policies=True)
                    except SystemExit:
                        pass
                result = None
        finally:
            pc.install(None)
            monkeypatch.setattr(pt, "load_file", orig_file)
            monkeypatch.setattr(pt, "run_files_raw", orig_batch)
        counters = (cache.parses, list(cache.hits), cache.rejected_entries, cache.memory_error)
        return cache.new, counters, result, files, batches

    old_new, old_counters, old_result, old_files, _ = fill(batch=False)
    new_new, new_counters, new_result, new_files, batches = fill(batch=True)
    assert len(old_new) > 30  # the fill is not trivially empty
    assert list(new_new.items()) == list(old_new.items())
    assert new_counters == old_counters
    assert new_result == old_result
    # Every file the per-file fill ran file mode for, the batch carried
    # (refused files included: never cached, still parsed once) …
    assert set(old_files) <= set(new_files) | {r for b in batches for r in b}
    assert {"broken.yaml", "listy.yaml", "not-utf8.yaml", "team/sub/bad-dup.yaml"} <= {
        r for b in batches for r in b}
    # … in at most two runs (every routing read and tenant-file read; then
    # the tenant files whose read raised, which nothing caches, read once
    # more by `declared_tenants`), and file mode alone only for the carrier
    # verdicts of `team/` (two spellings there).
    assert 1 <= len(batches) <= 2
    assert set(new_files) <= {"team/_defaults.yaml", "team/_defaults.yml"}, new_files


def _spied_reader(d: Path, monkeypatch):
    """A reader under a fresh cache, with every file-mode run recorded."""
    files = []
    orig = pt.load_file
    monkeypatch.setattr(pt, "load_file", lambda *a, **k: files.append(a[1]) or orig(*a, **k))
    return pc.ConfdReader(d), files


def test_a_stashed_line_serves_only_its_own_cache_key(tmp_path, monkeypatch):
    """The stash is keyed as the cache is: a line prefetched for a read is
    taken only by that read of those bytes, once. A sibling carrier that
    changes between prefetch and the carrier verdict — whose cache key holds
    every carrier of the directory — takes file mode on its own, and so
    does a file whose own bytes changed; the verdict and the value are
    those of the tree at the time of the read."""
    d = _write_tree(tmp_path, {
        "team/_defaults.yaml": "defaults: {a: 1}\n",
        "team/_defaults.yml": "defaults: {a: 2}\n",
        "team/t.yaml": "tenants: {t: {}}\n",
        "u.yaml": "tenants: {u: {}}\n",
    })
    cache = pc.ParseCache()
    pc.install(cache)
    try:
        reader, files = _spied_reader(d, monkeypatch)
        mode = _grar_parse.ROUTING_READ
        reader.prefetch([d / "team/_defaults.yaml", d / "team/_defaults.yml",
                         d / "team/t.yaml", d / "u.yaml"], mode)
        assert len(reader._stash) == 4
        # At prefetch `.yml` is the unselected spelling (`.yaml` wins). The
        # sibling is removed, so `.yml` is now its directory's carrier; the
        # stashed line, made beside `.yaml`, would still say unselected.
        stale = pt.tree_of(pt.file_document(reader._stash[(pc.content_key(
            "team/_defaults.yml", b"defaults: {a: 2}\n"), mode.name)]))
        assert stale.not_read.get("team/_defaults.yml") == "unselected_defaults_carrier"
        (d / "team/_defaults.yaml").unlink()
        verdict = reader._verdict("team/_defaults.yml")
        assert files == ["team/_defaults.yml"]  # file mode, not the stashed line
        assert verdict == (None, {"team": "team/_defaults.yml"})
        # A file rewritten after the prefetch: its read misses the stash.
        (d / "u.yaml").write_text("tenants: {u2: {}}\n", encoding="utf-8")
        assert reader.read(d / "u.yaml", mode) == {"tenants": {"u2": {}}}
        assert files[-1] == "u.yaml"
        # An unchanged file: the stashed line, taken once.
        n = len(files)
        assert reader.read(d / "team/t.yaml", mode) == {"tenants": {"t": {}}}
        assert len(files) == n
        assert (pc.content_key("team/t.yaml", b"tenants: {t: {}}\n"), mode.name) \
            not in reader._stash
    finally:
        pc.install(None)


def test_the_stash_is_empty_after_the_fill(tmp_path, monkeypatch):
    """Every line a generator run stashes is taken by its read: nothing is
    held for the reader's lifetime."""
    monkeypatch.setenv("PATH", os.devnull)
    d = _write_cold_tree(tmp_path)
    readers = []
    orig_init = pc.ConfdReader.__init__

    def track(self, *a, **k):
        orig_init(self, *a, **k)
        readers.append(self)

    monkeypatch.setattr(pc.ConfdReader, "__init__", track)
    _lib_confd.reset_warned_for_test()
    cache = pc.ParseCache()
    pc.install(cache)
    try:
        _run_findings(d, tmp_path / "f.json")
    finally:
        pc.install(None)
    assert readers and len(cache.new) > 30
    assert all(r._stash == {} for r in readers), [len(r._stash) for r in readers]
