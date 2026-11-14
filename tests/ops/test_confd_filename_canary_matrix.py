"""#3703：conf.d 的檔名與目錄名不進訊息——route generator 的 canary 守衛。

`test_routing_value_canary_matrix.py`、`test_reserved_value_canary_matrix.py` 守
的是設定作者寫的「值」與「鍵名」；這裡守作者決定的另一種文字：conf.d 樹裡的
檔名與目錄名。規則：一個不合法的名字（`_lib_validation.configmap_key_problem`
不放行的，例如含空白或 `@`）不會原樣出現在 generator 的 stdout、stderr 與
`--findings-json` 任何欄位；合法的照印。唯一出口是 `_lib_validation.shown_file`，
`Finding.file` 在建構子過它。

情境與接線都不靠人挑，兩道守衛各抓一種漏：

1. **語料由既有案例推導（抓「沒接出口」）**：`_grar_findings_cases.CASES`
   （加上它的 `UNITEMISED`）的每個案例照原樣建樹、再機械地改名建一次——每個目錄、每個不以 `_` 開頭的
   `.yaml`／`.yml` 檔換成帶序號的不合法名字（含空白與 `@`，副檔名保留；`_`
   開頭的檔名決定它的角色，不改，所在目錄照改）——同一模式各跑一次，斷言
   (a) rc 與 findings 的 kind 多重集合相同：改名沒改變被判到的東西；(b)
   改名那次的輸出裡沒有 canary。案例集新增案例自動被涵蓋。
2. **每個出口呼叫點都要被走到（抓「接了但沒人守」）**：以 AST 列出產生器
   各模組裡每個 `shown_file(` 呼叫點；上面那些改名的跑法在子行程裡以
   in-process 方式執行產生器（`runpy`，與 CLI 同一條路），`shown_file` 換成
   記錄用的包裝（呼叫者的檔案、行、欄；這次是否真的印了替身）。每個呼叫點
   至少要被一個情境走到**且印過替身**；走不到的只能列在 `_UNREACHED`（附理
   由），列了卻被走到、或呼叫點已不存在，都紅。

改名做不出來的條目形狀（symlink、FIFO、非 UTF-8 的名字、`_` 開頭但不是載體
名的檔）另列在 `_ENTRY_SHAPES`，它們的跑法也算進第 2 道的涵蓋。
"""
from __future__ import annotations

import ast
import functools
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
TOOLS = REPO / "scripts" / "tools"
OPS = TOOLS / "ops"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "tests"))
import _grar_findings_cases as cases  # noqa: E402
from _lib_validation import shown_file, shown_key  # noqa: E402
from _platform_fs import symlink_or_skip  # noqa: E402

_GAR = OPS / "generate_alertmanager_routes.py"
_CANARY = "zqx"  # every canary name holds it; compared lower-cased
_YAML = (".yaml", ".yml")
#: Every tree of the corpus, and every (tree, mode) the guards run.
_TREES = {**cases.FIXTURES, **cases.UNITEMISED}
_RUNS = [(f, m) for f in _TREES for m in cases.MODES]

#: The modules whose `shown_file(` calls the second guard accounts for.
_SITE_FILES = (_GAR, *sorted(OPS.glob("_grar_*.py")), TOOLS / "_lib_key_verdict.py")


# ── the rename (guard 1's corpus) ─────────────────────────────────────────

def canary_names(files: "dict[str, str]") -> "dict[str, str]":
    """Each relative path of *files* → the same path with every directory and
    every config file that does not start with `_` renamed to a numbered name
    that is not a legal file name (a space and `@`; the extension kept). A
    `_` file keeps its name — it names the file's role (`_defaults.yaml`,
    `_domain_policy.yaml`, …) — but its directory is renamed."""
    dirs = sorted({"/".join(rel.split("/")[:i]) for rel in files
                   for i in range(1, rel.count("/") + 1)})
    new_dir = {"": ""}
    for n, d in enumerate(dirs, 1):
        parent = d.rpartition("/")[0]
        new_dir[d] = (new_dir[parent] + "/" if parent else "") + f"ZqX D{n}@c"
    out, n = {}, 0
    for rel in sorted(files):
        parent, _, base = rel.rpartition("/")
        stem, dot, ext = base.rpartition(".")
        if not base.startswith("_") and f".{ext}" in _YAML and dot:
            n += 1
            base = f"ZqX F{n}@c.{ext}"
        out[rel] = (new_dir[parent] + "/" if parent else "") + base
    return out


def _referenced_names(files: "dict[str, str]") -> "list[str]":
    """Renamed paths a fixture's own text mentions (`a.yaml`, `team/`): the
    rename would have to change that text too, which it does not do — such a
    fixture fails `test_the_rename_needs_no_text_change` until it is taught."""
    names = canary_names(files)
    said = set()
    for rel, new in names.items():
        if rel == new:
            continue
        base = rel.rpartition("/")[2]
        if not base.startswith("_"):
            said.add(base)
        said.update(seg + "/" for seg in rel.split("/")[:-1])
    return sorted(s for s in said for body in files.values() if s in body)


# ── one run, with every `shown_file` call recorded ────────────────────────

#: Runs the generator as its CLI does (`runpy`, `__main__`) with
#: `_lib_validation.shown_file` replaced, before any module binds it, by a
#: wrapper that appends [caller file, line, column, printed a stand-in] for
#: every call. argv: tools dir, generator, hits file, the generator's argv.
_DRIVER = r"""
import json, runpy, sys
tools, gar, hits = sys.argv[1:4]
sys.argv = [gar, *sys.argv[4:]]
sys.path.insert(0, tools)
import _lib_validation
_shown_file = _lib_validation.shown_file
def shown_file(path):
    out = _shown_file(path)
    caller = sys._getframe(1)
    code = caller.f_code
    line, _end, col, _endcol = list(code.co_positions())[caller.f_lasti // 2]
    with open(hits, "a", encoding="utf-8") as fh:
        fh.write(json.dumps([code.co_filename, line, col,
                             path is not None and out != str(path)]) + "\n")
    return out
_lib_validation.shown_file = shown_file
runpy.run_path(gar, run_name="__main__")
"""


class Outcome(NamedTuple):
    rc: int
    kinds: "tuple[tuple[str, int], ...]"   # sorted Counter items
    text: str                               # stdout + stderr + the findings file
    hits: "tuple[tuple[str, int, int, bool], ...]"


def _drive(conf: Path, argv: "list[str]") -> Outcome:
    work = conf.parent
    out, hits = work / "findings.json", work / "hits.jsonl"
    for p in (out, hits):
        if p.exists():
            p.unlink()
    env = {k: v for k, v in os.environ.items() if k != "PATH"}
    env["PATH"] = os.devnull  # nothing resolvable on PATH, as cases.run_gar
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(
        [sys.executable, "-s", "-c", _DRIVER, str(TOOLS), str(_GAR), str(hits),
         "--config-dir", str(conf), *argv, "--findings-json", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, env=env)
    assert "Traceback" not in p.stderr, p.stderr
    assert out.exists(), p.stderr
    written = out.read_text(encoding="utf-8", errors="replace")
    kinds = Counter(f["kind"] for f in json.loads(written)["findings"])
    rows = (tuple(json.loads(line)) for line in
            (hits.read_text(encoding="utf-8").splitlines() if hits.exists() else ()))
    return Outcome(p.returncode, tuple(sorted(kinds.items())),
                   p.stdout + p.stderr + written, tuple(rows))


@functools.lru_cache(maxsize=None)
def _case(fixture: str, mode: str, renamed: bool) -> Outcome:
    files = _TREES[fixture]
    names = canary_names(files) if renamed else {rel: rel for rel in files}
    with tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "conf.d"
        for rel, body in files.items():
            p = conf / names[rel]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        return _drive(conf, cases.MODES[mode])


#: Fixtures whose finding kinds depend on a file's NAME, so the rename may
#: change them (fixture → why). None today.
_KINDS_DEPEND_ON_NAME: "dict[str, str]" = {}


def test_the_rename_needs_no_text_change():
    """The rename moves files, not text: no fixture may mention a path it
    renames (else the renamed tree is a different tree)."""
    said = {f: _referenced_names(files) for f, files in _TREES.items()}
    assert not {f: s for f, s in said.items() if s}, said


def test_every_canary_name_is_one_shown_file_replaces():
    for files in _TREES.values():
        for rel, new in canary_names(files).items():
            for old_seg, seg in zip(rel.split("/"), new.split("/")):
                if old_seg != seg:
                    assert _CANARY in seg.lower() and shown_file(seg) != seg, seg


@pytest.mark.parametrize("fixture,mode", _RUNS, ids=[f"{f}/{m}" for f, m in _RUNS])
def test_a_renamed_tree_is_judged_alike_and_prints_no_name(fixture, mode):
    plain, renamed = _case(fixture, mode, False), _case(fixture, mode, True)
    if fixture not in _KINDS_DEPEND_ON_NAME:
        assert (renamed.rc, renamed.kinds) == (plain.rc, plain.kinds), renamed.text[-3000:]
    assert _CANARY not in renamed.text.lower(), renamed.text


# ── entry shapes the rename cannot make ──────────────────────────────────

_ROOT = "defaults:\n  cpu_usage_percent: 80\n"


def _tenant(tid: str, extra: str = "") -> str:
    return f"tenants:\n  {tid}:\n    cpu_usage_percent: \"85\"\n{extra}"


#: name → (files, what proves the place was judged, the paths whose stand-in
#: the output shows). A file name `@symlink:<name>` is a dangling symlink to
#: its body, `@fifo:<name>` a FIFO, `@bytes:<name>` a name that is not valid
#: UTF-8. The proof is ("kind", k): a finding of kind k; or ("text", s): a
#: line of stdout / stderr holding s (a place only warned about on stderr).
_ENTRY_SHAPES: "dict[str, tuple[dict[str, str], tuple[str, str], tuple[str, ...]]]" = {
    # Not a regular file: the rename makes only regular files.
    "special": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                 "@symlink:ZqX LINK@x.yaml": "nowhere-zqx.yaml",
                 "@fifo:ZqX FIFO@x.yaml": ""},
                ("kind", "tenant_file_unreadable"), ("ZqX LINK@x.yaml", "ZqX FIFO@x.yaml")),
    # A `_` file whose name is not a carrier's: the rename keeps `_` names.
    "platform": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                  "_ZqX PLAT@x.yaml": "_routing_defaults:\n  receiver: 1\n",
                  "ZqX NOTE@x.txt": "hello\n"},
                 ("kind", "missing_receiver_field"), ("_ZqX PLAT@x.yaml",)),
    "policy_misplaced": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                          "_ZqX POL@x.yaml": "domain_policies:\n  d: {tenants: [db-ok]}\n"},
                         ("kind", "domain_policy_unusable"), ("_ZqX POL@x.yaml",)),
    "orphan_platform": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                         "_ZqX PLAT@x.yaml": "tenants:\n  db-zz:\n    cpu_usage_percent: '1'\n"},
                        ("text", "ignored — no tenant file declares tenant 'db-zz'"),
                        ("_ZqX PLAT@x.yaml",)),
    "enforced_below_root": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                             "ZqX DIR@y/_ZqX E@x.yaml": "_routing_enforced:\n  enabled: true\n"},
                            ("kind", "routing_enforced_below_root"),
                            ("ZqX DIR@y/_ZqX E@x.yaml",)),
    # A name that is not valid UTF-8: da-guard refuses the whole tree, and
    # the generator's rc-2 line repeats da-guard's stderr, which quotes the
    # name. Known: see `_KNOWN`.
    "not_utf8_name": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                       "@bytes:ZqX\udcff@x.yaml": _tenant("db-x")},
                      ("text", "cannot read the conf.d through `da-guard parse-tree`"), ()),
}

#: Cells that break the rule today, as xfail(strict=True): the day one is
#: fixed it XPASSes and goes red until its entry is deleted.
_KNOWN = {
    "not_utf8_name": (
        "#3703 open, owner ruling: `parse_tree_failure_exit` repeats da-guard's "
        "stderr, which quotes the file name; tests/shared/test_confd_behaviour_matrix.py "
        "(ADR-036 §7, `refused` row) requires every tool to name that file, so "
        "not printing it breaks that contract"),
}

#: Entry shapes also rendered (`--output-alertmanager`).
_RENDERED = ("special", "not_utf8_name")


def _params(names):
    return [pytest.param(n, marks=[pytest.mark.xfail(strict=True, raises=AssertionError,
                                                     reason=_KNOWN[n])] if n in _KNOWN else [])
            for n in names]


def _build(d: Path, files: "dict[str, str]") -> None:
    for name, body in files.items():
        kind, _, rel = name.rpartition(":") if name.startswith("@") else ("", "", name)
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if kind == "@symlink":
            symlink_or_skip(body, p)
            continue
        try:
            if kind == "@fifo":
                os.mkfifo(p)
            elif kind == "@bytes":
                with open(os.fsencode(p), "w", encoding="utf-8") as fh:
                    fh.write(body)
            else:
                p.write_text(body, encoding="utf-8")
        except (AttributeError, NotImplementedError, OSError) as e:
            # No os.mkfifo (Windows), or a filesystem that refuses a name
            # that is not valid UTF-8 (APFS).
            pytest.skip(f"this platform cannot make {kind or 'file'} {rel!r}: {e}")


def _shape(name: str, argv: "tuple[str, ...]") -> Outcome:
    files = _ENTRY_SHAPES[name][0]
    with tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "conf.d"
        conf.mkdir()
        _build(conf, files)
        res = _drive(conf, list(argv))
    # A tree da-guard refuses whole is the run's caller error (rc 2,
    # `run_failed`): that is what `not_utf8_name` measures; anywhere else a
    # run that judged nothing has no canary to find.
    if "@bytes:" not in "".join(files):
        assert "run_failed" not in dict(res.kinds), res.text[-3000:]
    return res


@pytest.mark.parametrize("name", _params(sorted(_ENTRY_SHAPES)))
def test_an_entry_shape_prints_no_name_under_validate(name):
    _files, (how, what), shows = _ENTRY_SHAPES[name]
    res = _shape(name, ("--validate", "--strict"))
    if how == "kind":
        assert what in dict(res.kinds), (what, res.kinds)
    else:
        assert what in res.text, (what, res.text[-3000:])
    for rel in shows:
        assert shown_file(rel) in res.text, (rel, shown_file(rel), res.text[-3000:])
    assert _CANARY not in res.text.lower(), res.text


@pytest.mark.parametrize("name", _params(_RENDERED))
def test_an_entry_shape_prints_no_name_under_render(name):
    res = _shape(name, ("--output-alertmanager", "--dry-run"))
    for rel in _ENTRY_SHAPES[name][2]:
        assert shown_file(rel) in res.text, (rel, res.text[-3000:])
    assert _CANARY not in res.text.lower(), res.text


# ── guard 2: every call site is reached with a name it replaces ───────────

def _call_sites() -> "dict[str, tuple[str, int, int]]":
    """`<file>:<function>#<n>` (the n-th `shown_file(` call in that function,
    in source order; `<module>` at top level) → (absolute file, line, column)
    of the call, as `co_positions` reports it."""
    sites: "dict[str, tuple[str, int, int]]" = {}
    for path in _SITE_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner: "dict[int, str]" = {}

        def visit(node, qual):
            for child in ast.iter_child_nodes(node):
                q = qual
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    q = f"{qual}.{child.name}" if qual != "<module>" else child.name
                if isinstance(child, ast.Call):
                    owner[id(child)] = q
                visit(child, q)

        visit(tree, "<module>")
        calls = sorted((n for n in ast.walk(tree) if isinstance(n, ast.Call)
                        and getattr(n.func, "id", getattr(n.func, "attr", None)) == "shown_file"),
                       key=lambda n: (n.lineno, n.col_offset))
        seen: "Counter[str]" = Counter()
        for c in calls:
            q = owner[id(c)]
            seen[q] += 1
            sites[f"{path.name}:{q}#{seen[q]}"] = (str(path), c.lineno, c.col_offset)
    return sites


#: Call sites no run of the corpus reaches with a name it replaces, each with
#: why. Keep it empty: a case that reaches one belongs in `CASES`.
_UNREACHED: "dict[str, str]" = {
    "_grar_parse.py:_parse_config_files#2":
        "#2981: `_GIT_IRREGULAR` holds the entries the generator service read "
        "from git as not regular files; the CLI never fills it",
    "_grar_parse.py:credential_findings#2":
        "the NOTICE for a credential in a file the write does not change: only "
        "the generator service's write passes `credential_scope`; the CLI never does",
}


def _corpus() -> "list[Outcome]":
    jobs = [functools.partial(_case, f, m, True) for f, m in _RUNS]
    jobs += [functools.partial(_shape, n, ("--validate", "--strict"))
             for n in _ENTRY_SHAPES if n not in _KNOWN]
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(lambda job: job(), jobs))


def test_every_shown_file_call_site_is_reached_with_a_name_it_replaces():
    sites = _call_sites()
    assert len(sites) >= 40, sorted(sites)  # the derivation did not collapse
    where = {loc: key for key, loc in sites.items()}
    targets = {str(p) for p in _SITE_FILES}
    replaced, unknown = set(), set()
    for res in _corpus():
        for file, line, col, stood in res.hits:
            key = where.get((os.path.realpath(file), line, col)) or where.get((file, line, col))
            if key is None:
                if os.path.realpath(file) in targets:
                    unknown.add((file, line, col))
                continue
            if stood:
                replaced.add(key)
    missing = sorted(set(sites) - replaced - set(_UNREACHED))
    stale = sorted(set(_UNREACHED) & replaced)
    gone = sorted(set(_UNREACHED) - set(sites))
    assert not unknown, f"calls the AST walk does not list (the mapping broke): {sorted(unknown)}"
    assert not missing, (
        f"`shown_file(` call sites no case reaches with an illegal name: {missing}. "
        "Add a case to tests/ops/_grar_findings_cases.py that reaches each, or a "
        "reason to _UNREACHED")
    assert not stale, f"_UNREACHED entries a case now reaches (delete them): {stale}"
    assert not gone, f"_UNREACHED entries whose call site is gone: {gone}"


# ── legal names are printed as written ────────────────────────────────────

_UNK = "    nosuch_key_zz: \"1\"\n"
_LONG_A, _LONG_B = "a" * 63, "b" * 63


def test_legal_names_are_printed_as_written(tmp_path):
    """`db-x.yaml`, `team-a/db-y.yaml`, and a 63-char tenant id's `<id>.yaml`
    (68 chars) and `<id>.yml`: `file` is the path relative to the conf.d root,
    as written (tenant-api's `Related` compares it with the path it wrote)."""
    files = {"_defaults.yaml": _ROOT, "db-x.yaml": _tenant("db-x", _UNK),
             "team-a/db-y.yaml": _tenant("db-y", _UNK),
             f"{_LONG_A}.yaml": _tenant(_LONG_A, _UNK),
             f"team-a/{_LONG_B}.yml": _tenant(_LONG_B, _UNK)}
    conf = tmp_path / "conf.d"
    _build(conf, files)
    res = _drive(conf, ["--validate", "--strict"])
    findings = json.loads((tmp_path / "findings.json").read_text(encoding="utf-8"))["findings"]
    got = {f["tenant"]: f["file"] for f in findings if f["kind"] == "key_unread"}
    assert got == {"db-x": "db-x.yaml", "db-y": "team-a/db-y.yaml",
                   _LONG_A: f"{_LONG_A}.yaml", _LONG_B: f"team-a/{_LONG_B}.yml"}, findings
    assert "<key:" not in res.text, res.text
    assert not any(stood for *_, stood in res.hits), res.hits


# ── shown_file itself ───────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "db-x.yaml", "team-a/db-y.yaml", f"{'a' * 63}.yaml", f"{'a' * 63}.yml",
    "_defaults.yaml", ".hidden.yaml", "a" * 253, "x/" + "a" * 253,
])
def test_shown_file_prints_a_legal_path_as_written(path):
    assert shown_file(path) == path


@pytest.mark.parametrize("segment", [
    "a" * 254, "..", ".", "..x", "a b", "x@y.yaml", "", "ZqX\udcff.yaml", "é.yaml",
], ids=lambda s: ascii(s)[:24])  # ascii(): a lone surrogate in a test id breaks xdist
def test_shown_file_replaces_an_illegal_segment(segment):
    got = shown_file(f"team-a/{segment}/db-x.yaml")
    # shown_key's stand-in: one format, one digest — also for a segment
    # shown_key itself would print (`..`, 254 chars of a name-like charset).
    digest = hashlib.sha256(segment.encode("utf-8", "surrogatepass")).hexdigest()
    # A bool and ascii(): a failure report holding a lone surrogate (pytest's
    # comparison explanation included) breaks xdist too.
    ok = got == f"team-a/<key:{digest[:6]}>/db-x.yaml"
    assert ok, ascii(got)


def test_shown_file_and_shown_key_share_the_stand_in():
    assert shown_file("a b") == shown_key("a b") != "a b"


@pytest.mark.parametrize("path", [
    None, "db-x.yaml", "ZqX DIR@y/db-x.yaml", "a b/c d/e@f", "..", "a" * 254 + "/x.yaml",
])
def test_shown_file_is_idempotent(path):
    once = shown_file(path)
    assert shown_file(once) == once
    assert once is None or _CANARY not in once.lower()


def test_a_finding_rebuilt_from_a_shown_file_keeps_it():
    """`Finding` puts `file` through `shown_file` in its constructor, and
    `with_text` / a pickle rebuild hand the shown `file` back to it: the
    stand-in survives as it is, not a stand-in of a stand-in."""
    import pickle
    sys.path.insert(0, str(OPS))
    from _grar_merge import Finding

    f = Finding("x", kind="k", blocks="never", file="ZqX DIR@y/db-x.yaml")
    assert f.file == shown_file("ZqX DIR@y/db-x.yaml") != "ZqX DIR@y/db-x.yaml"
    assert f.with_text("y").file == f.file
    assert pickle.loads(pickle.dumps(f)).file == f.file
    assert Finding("x", kind="k", blocks="never", file=f"{_LONG_A}.yaml").file == f"{_LONG_A}.yaml"
