"""#3216: one JSON encoder; a --json run's document goes out through run().

A tool hands its document back (`return Report(doc)`); `_lib_exitcodes.run`
encodes it with `encode_json` — two-space indent, no NaN / Infinity, `date` /
`datetime` as ISO 8601, a type's own `__json__()` — and prints it. A value
the encoder cannot carry is the `output_not_serializable` envelope at the
crash exit code, never a document with `Infinity` in it, never part of one.

The gate below is the ratchet that moves tools onto it: every file that
still prints its own JSON is listed with how many times it does, and the
count may only go down.
"""
from __future__ import annotations

import ast
import datetime
import json
import math
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"
sys.path.insert(0, str(TOOLS))
import _lib_exitcodes as lib  # noqa: E402
from _lib_parse_tree import Timestamp  # noqa: E402


# ── the encoder ──────────────────────────────────────────────────────────

def test_one_format_two_space_utf8():
    assert lib.encode_json({"名": [1, 2.5, None, True]}) == (
        '{\n  "名": [\n    1,\n    2.5,\n    null,\n    true\n  ]\n}')


def test_dates_are_iso_8601_and_a_timestamp_is_its_text():
    doc = {"d": datetime.date(2024, 1, 2), "t": datetime.datetime(2024, 1, 2, 3, 4, 5),
           "y": Timestamp("2001-12-14 21:59:43.10 -5")}
    assert json.loads(lib.encode_json(doc)) == {
        "d": "2024-01-02", "t": "2024-01-02T03:04:05", "y": "2001-12-14 21:59:43.10 -5"}


@pytest.mark.parametrize("doc, where", [
    ({"changes": [{}, {}, {}, {"impact_pct": math.inf}]}, "changes[3].impact_pct"),
    ({"x y": {"z": math.nan}}, '["x y"].z'),
    ([-math.inf], "[0]"),
    ({"s": {1, 2}}, "s"),
    ({"p": pathlib.Path("/x")}, "p"),
    ({("t",): 1}, "[('t',)]"),
    ({1.5: 0}, "[1.5]"),                       # not the str key "1.5"
    ({"a": {1: math.nan}}, 'a["1"]'),          # an int key is the JSON key it prints as …
    ({"a": [0, math.nan]}, "a[1]"),            # … not a list index
    ({True: 1, "true": 2}, "true"),            # two keys, one JSON key
    ({1: "a", "1": "b"}, '["1"]'),
    ({"n": 10 ** 5000}, "n"),                  # too long for int → str
])
def test_what_json_cannot_carry_is_named_where_it_is(doc, where):
    with pytest.raises(lib.OutputNotSerializable) as exc:
        lib.encode_json(doc)
    assert exc.value.where == where


def _cycle_list():
    a: list = []
    a.append(a)
    return a


def _cycle_dict():
    d: dict = {}
    d["k"] = {"back": d}
    return d


class _Self:
    def __json__(self):
        return self


@pytest.mark.parametrize("doc, where", [
    (_cycle_list(), "[0]"), (_cycle_dict(), "k.back"), ({"s": _Self()}, "s")])
def test_a_cycle_is_named_not_a_hang(doc, where):
    with pytest.raises(lib.OutputNotSerializable) as exc:
        lib.encode_json(doc)
    assert exc.value.where == where


def test_a_shared_value_is_not_a_cycle():
    shared = {"x": 1}
    assert json.loads(lib.encode_json({"a": shared, "b": [shared, shared]})) == {
        "a": {"x": 1}, "b": [{"x": 1}, {"x": 1}]}


def test_what_is_checked_is_what_is_printed():
    """`__json__` runs once, and wins over a container's own items."""
    class Flaky:
        calls = 0

        def __json__(self):
            Flaky.calls += 1
            return 1 if Flaky.calls == 1 else math.nan

    class D(dict):
        def __json__(self):
            return {"safe": 1}

    assert json.loads(lib.encode_json({"f": Flaky(), "d": D(x=math.nan)})) == {
        "f": 1, "d": {"safe": 1}}


def test_a_deep_document_is_not_a_recursion_error():
    doc: list = []
    for _ in range(5000):
        doc = [doc]
    assert lib.encode_json(doc).count("[") == 5001


def test_a_json_hook_does_not_smuggle_a_value_past_the_rules():
    class Sly:
        def __json__(self):
            return {"v": math.nan}
    with pytest.raises(lib.OutputNotSerializable) as exc:
        lib.encode_json({"a": Sly()})
    assert exc.value.where == "a.v"


# ── run() writes the document ────────────────────────────────────────────

def _run(body: str):
    script = ("import sys; sys.path.insert(0, %r)\n"
              "import math, _lib_exitcodes as lib\n" % str(TOOLS)) + body
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          encoding="utf-8", timeout=60)


def test_run_prints_the_document_and_exits_with_its_status():
    p = _run("lib.run(lambda: lib.Report({'n': 1}, 1))")
    assert p.returncode == 1, p.stderr
    assert json.loads(p.stdout) == {"n": 1}


def _armed(body: str) -> str:
    return f"def main():\n    lib.arm_crash_envelope()\n{body}\nlib.run(main)"


@pytest.mark.real_crash_exit_code
def test_a_document_json_cannot_carry_is_the_named_envelope_not_part_of_it():
    p = _run(_armed("    return lib.Report({'ok': [1], 'bad': {'x': math.inf}})"))
    assert p.returncode == 2, p.stderr
    doc = json.loads(p.stdout)  # only the envelope: no `"ok"` printed before it
    assert (doc["status"], doc["reason"]) == ("caller_error", "output_not_serializable")
    assert "bad.x" in doc["error"] and "ok" not in doc
    assert "bad.x: inf is not a JSON number" in p.stderr
    assert "Traceback" not in p.stderr


@pytest.mark.real_crash_exit_code
@pytest.mark.parametrize("armed, printed_first, envelope", [
    (True, False, True), (True, True, False), (False, False, False), (False, True, False)])
def test_the_envelope_follows_the_armed_rule(armed, printed_first, envelope):
    """Like every other rc-2 exit: an envelope only when armed and the tool
    has not written to stdout itself — unarmed is not armed on the spot."""
    body = ("    print('NOTICE: x')\n" if printed_first else "") + "    return lib.Report([math.nan])"
    p = _run(_armed(body) if armed else f"def main():\n{body}\nlib.run(main)")
    assert p.returncode == 2, p.stderr
    assert ('"output_not_serializable"' in p.stdout) is envelope, p.stdout
    assert "[0]: nan is not a JSON number" in p.stderr


@pytest.mark.parametrize("status", ["bad", None, True, 300, -1])
def test_a_report_status_is_an_exit_code(status):
    with pytest.raises(ValueError):
        lib.Report({}, status)


@pytest.mark.real_crash_exit_code
def test_the_tools_own_crash_code_is_kept():
    """tenant-verify's sanctioned crash code (1) is what a defect exits with."""
    p = _run("lib.set_crash_exit_code(1)\nlib.run(lambda: lib.Report([math.nan]))")
    assert p.returncode == 1, p.stderr


def test_in_process_callers_take_the_same_step(capsys):
    assert lib.emit_result(lib.Report({"a": datetime.date(2024, 1, 2)})) == 0
    assert json.loads(capsys.readouterr().out) == {"a": "2024-01-02"}
    assert lib.emit_result(3) == 3


# ── the ratchet: tools that still print their own JSON ───────────────────

#: ⛔ scripts/tools file -> how many times it prints its own JSON: `print` /
#: `sys.stdout.write` / `click.echo` of JSON text (see `_JsonText`: under any
#: import alias, through a formatter found by `json_formatters`, a local
#: holding it, `+` or an f-string), or `json.dump(..., sys.stdout)`. Migrate a
#: site to `return Report(doc)` and lower its count here; a file not listed
#: may not do it at all. Blind spots, by choice: a method returning JSON text,
#: and a value passed through a call or an attribute before it is printed.
OWN_JSON = {
    "_lib_doc_yaml_inventory.py": 1,
    "ops/alert_correlate.py": 1,
    "ops/alert_quality.py": 2,
    "ops/analyze_rule_pack_gaps.py": 1,
    "ops/assemble_config_dir.py": 7,
    "ops/backtest_threshold.py": 8,
    "ops/batch_diagnose.py": 2,
    "ops/blind_spot_discovery.py": 1,
    "ops/byo_check.py": 1,
    "ops/cardinality_forecasting.py": 3,
    "ops/check_alert.py": 3,
    "ops/config_diff.py": 1,
    "ops/cutover_tenant.py": 1,
    "ops/diagnose.py": 3,
    "ops/discover_instance_mappings.py": 4,
    "ops/drift_detect.py": 1,
    "ops/explain_keys.py": 2,
    "ops/federation_check.py": 1,
    "ops/generate_rule_pack_split.py": 1,
    "ops/gitops_check.py": 2,
    "ops/grafana_import.py": 2,
    "ops/maintenance_scheduler.py": 1,
    "ops/notification_tester.py": 2,
    "ops/onboard_platform.py": 1,
    "ops/operator_check.py": 1,
    "ops/operator_generate.py": 1,
    "ops/policy_engine.py": 7,
    "ops/policy_opa_bridge.py": 4,
    "ops/rule_pack_diff.py": 1,
    "ops/runtime_audit.py": 2,
    "ops/shadow_verify.py": 1,
    "ops/silencer_drift_check.py": 1,
    "ops/state_reconcile.py": 1,
    "ops/threshold_govern.py": 1,
    "ops/threshold_recommend.py": 2,
    "ops/validate_config.py": 1,
    "validate_all.py": 3,
}


_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _own_nodes(scope: ast.AST):
    """The nodes of *scope* that are not inside a function nested in it."""
    todo = list(ast.iter_child_nodes(scope))
    while todo:
        n = todo.pop()
        yield n
        if not isinstance(n, _FUNCS):
            todo.extend(ast.iter_child_nodes(n))


def _scopes(tree: ast.Module):
    """The module and every function in it, each with its own nodes."""
    yield tree, list(_own_nodes(tree))
    for fn in ast.walk(tree):
        if isinstance(fn, _FUNCS):
            yield fn, list(_own_nodes(fn))


class _JsonText:
    """Which expressions of one scope are JSON text, as far as the file says:
    a call of ``json.dumps`` (under any name it was imported as) or of a JSON
    formatter (resolved the way Python would: a function of this file, or
    one imported from the module that defines it), a name the scope
    assigned one, or a ``+`` / f-string built on one."""

    def __init__(self, tree: ast.Module, stem: str, formatters: dict[str, set[str]]) -> None:
        self.json_mods = {"json"}
        self.dumps: set[str] = set()
        self.dump: set[str] = set()
        self.names: set[str] = set()
        self.mods: dict[str, str] = {}  # local alias -> module stem
        local = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.fmt = set(formatters.get(stem, ()))
        self.formatters = formatters
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "json":
                        self.json_mods.add(a.asname or "json")
                    else:
                        self.mods[a.asname or a.name.split(".")[0]] = a.name.split(".")[-1]
            elif isinstance(node, ast.ImportFrom):
                mod = (node.module or "").split(".")[-1]
                for a in node.names:
                    name = a.asname or a.name
                    if mod == "json" and a.name in ("dumps", "dump"):
                        (self.dumps if a.name == "dumps" else self.dump).add(name)
                    elif a.name in formatters.get(mod, ()) and name not in local:
                        self.fmt.add(name)
                    elif a.name in formatters:  # `from pkg import module`
                        self.mods[name] = a.name

    def scope(self, nodes: list[ast.AST]) -> "_JsonText":
        """Taint the names *nodes* assign JSON text to, to a fixed point."""
        self.names = set()
        assigns = [n for n in nodes if isinstance(n, (ast.Assign, ast.AnnAssign))]
        grew = True
        while grew:  # a name assigned from a name assigned JSON text …
            grew = False
            for n in assigns:
                if n.value is None or not self.is_json(n.value):
                    continue
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    if isinstance(t, ast.Name) and t.id not in self.names:
                        self.names.add(t.id)
                        grew = True
        return self

    def _is(self, func: ast.expr, names: set[str], attr: str) -> bool:
        if isinstance(func, ast.Name):
            return func.id in names
        return (isinstance(func, ast.Attribute) and func.attr == attr
                and isinstance(func.value, ast.Name) and func.value.id in self.json_mods)

    def is_json(self, e: ast.expr) -> bool:
        if isinstance(e, ast.Call):
            f = e.func
            if self._is(f, self.dumps, "dumps") or (isinstance(f, ast.Name) and f.id in self.fmt):
                return True
            return (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                    and f.attr in self.formatters.get(self.mods.get(f.value.id, ""), ()))
        if isinstance(e, ast.Name):
            return e.id in self.names
        if isinstance(e, ast.BinOp):
            return self.is_json(e.left) or self.is_json(e.right)
        if isinstance(e, ast.JoinedStr):
            return any(isinstance(v, ast.FormattedValue) and self.is_json(v.value) for v in e.values)
        return False

    def prints_own(self, call: ast.Call) -> bool:
        f = ast.unparse(call.func)
        if f in ("print", "sys.stdout.write", "click.echo"):
            return any(self.is_json(a) for a in call.args)
        if self._is(call.func, self.dump, "dump"):
            fp = call.args[1] if len(call.args) > 1 else next(
                (k.value for k in call.keywords if k.arg == "fp"), None)
            return fp is not None and ast.unparse(fp) == "sys.stdout"
        return False


def _parsed(root: pathlib.Path):
    for path in sorted(root.rglob("*.py")):
        if path.name != "_lib_exitcodes.py":  # the encoder itself, and the envelope
            yield path, ast.parse(path.read_text(encoding="utf-8"))


def json_formatters(root: pathlib.Path = TOOLS) -> dict[str, set[str]]:
    """Module stem -> its module-level functions that return JSON text
    (``return json.dumps(…)``, or a formatter's answer, or a local holding
    one), to a fixed point — derived, not listed, so a new formatter is seen
    the day it is written. Blind spot, by choice: a method returning JSON
    text (``print(obj.to_json())``) — its bare name says nothing of which
    method a call reaches."""
    trees = [(path.stem, tree) for path, tree in _parsed(root)]
    found: dict[str, set[str]] = {}
    while True:
        grew = False
        for stem, tree in trees:
            text = _JsonText(tree, stem, found)
            # A formatter imported here is one of this module's names too
            # (`_lib_python` re-exports `_lib_io`'s).
            for name in text.fmt - found.get(stem, set()):
                found.setdefault(stem, set()).add(name)
                grew = True
            for fn, nodes in _scopes(tree):
                if (fn in tree.body and isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and fn.name not in found.get(stem, ())):
                    text.scope(nodes)
                    if any(isinstance(r, ast.Return) and r.value is not None and text.is_json(r.value)
                           for r in nodes):
                        found.setdefault(stem, set()).add(fn.name)
                        grew = True
        if not grew:
            return found


def own_json_counts(root: pathlib.Path = TOOLS) -> dict[str, int]:
    formatters = json_formatters(root)
    counts: dict[str, int] = {}
    for path, tree in _parsed(root):
        text = _JsonText(tree, path.stem, formatters)
        n = 0
        for _, nodes in _scopes(tree):
            text.scope(nodes)
            n += sum(1 for c in nodes if isinstance(c, ast.Call) and text.prints_own(c))
        if n:
            counts[path.relative_to(root).as_posix()] = n
    return counts


def test_no_tool_prints_more_of_its_own_json_than_listed():
    now = own_json_counts()
    grew = {f: (n, OWN_JSON.get(f, 0)) for f, n in now.items() if n > OWN_JSON.get(f, 0)}
    assert not grew, ("print a --json document by `return Report(doc)` (run() encodes it), "
                      f"not yourself — (now, allowed): {grew}")
    shrank = {f: (now.get(f, 0), n) for f, n in OWN_JSON.items() if now.get(f, 0) < n}
    assert not shrank, f"migrated — lower OWN_JSON to what is left (now, listed): {shrank}"


def test_the_ratchet_sees_each_shape(tmp_path):
    (tmp_path / "_lib_io.py").write_text(
        "import json\ndef format_json_report(d, **kw):\n    return json.dumps(d, **kw)\n",
        encoding="utf-8")
    (tmp_path / "fmt.py").write_text(
        "import json\ndef as_json(d):\n    return json.dumps(d)\n"
        "def render(d):\n    return json.dumps(d)\n", encoding="utf-8")
    (tmp_path / "t.py").write_text(
        "import json, sys, click\nimport json as J\nfrom json import dumps as D, dump\n"
        "from _lib_io import format_json_report\nfrom fmt import as_json as aj\nimport fmt\n"
        "def wrap(d):\n    out = format_json_report(d)\n    return out\n"
        "def main(d):\n"
        "    print(json.dumps(d))\n"            # 1
        "    print(format_json_report(d))\n"    # 2
        "    sys.stdout.write(json.dumps(d))\n"  # 3
        "    json.dump(d, sys.stdout)\n"         # 4
        "    print(json.dumps(d), file=sys.stderr)\n"  # 5: printed by the tool itself
        "    print(J.dumps(d))\n"                # 6: module alias
        "    print(D(d))\n"                      # 7: function alias
        "    text = json.dumps(d)\n    again = text\n    print(again)\n"  # 8: via locals
        "    sys.stdout.write(json.dumps(d) + '\\n')\n"  # 9: `+`
        "    print(f'{json.dumps(d)}')\n"        # 10: f-string
        "    dump(d, fp=sys.stdout)\n"           # 11: fp= keyword
        "    click.echo(json.dumps(d))\n"        # 12
        "    print(aj(d))\n"                     # 13: imported formatter, aliased
        "    print(fmt.as_json(d))\n"            # 14: module attribute
        "    print(wrap(d))\n"                   # 15: a formatter returning a local
        "    json.dump(d, open('x', 'w'))\n"     # a file, not stdout
        "    print(text.upper)\n"                 # not the text itself
        "def other():\n    print(text)\n",        # `text` is main's, not this scope's
        encoding="utf-8")
    (tmp_path / "u.py").write_text(
        # a `render` of its own (markdown) is not fmt's; a method is not seen
        "import json\ndef render(r):\n    return '# ' + r\n"
        "class R:\n    def to_json(self):\n        return json.dumps({})\n"
        "print(render('x'))\nprint(R().to_json())\n", encoding="utf-8")
    assert own_json_counts(tmp_path) == {"t.py": 15}


# ── #3144: explain-route on a value YAML reads as a date ─────────────────

@pytest.mark.usefixtures("da_guard_env")
def test_explain_route_writes_a_yaml_date_as_its_text(tmp_path):
    """#3144: an unquoted `team: 2001-12-15` was `date is not JSON
    serializable` (a crash, rc 2). It is the text as written now — what
    the generator and Alertmanager read — and the run is rc 0."""
    matrix = json.loads((REPO / "tests" / "shared" / "routing_policy_parity_matrix.json")
                        .read_text(encoding="utf-8"))
    [tree] = [t for t in matrix["trees"] if t["name"] == "routing-values-yaml11"]
    for rel, body in tree["files"].items():
        (tmp_path / rel).write_text(body, encoding="utf-8")
    assert "team: 2001-12-15" in tree["files"]["_platform.yaml"]
    p = subprocess.run([sys.executable, str(TOOLS / "ops" / "explain_route.py"),
                        "--config-dir", str(tmp_path), "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert p.returncode == 0, p.stderr[-3000:]
    assert '"2001-12-15"' in p.stdout
    json.loads(p.stdout)
