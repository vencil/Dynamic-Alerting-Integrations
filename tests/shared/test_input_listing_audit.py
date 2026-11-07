"""#3446 structural gate: a declared input directory is listed only by `_lib_input`.

Every argument a da-tools parser declares with ``_lib_input.add_input`` is
run here — its OPS_SITES row (``test_caller_paths.py``), the locked {D}
replaced by a sentinel directory that holds one file — under a start-up audit
hook (``_input_audit/sitecustomize.py``) that logs every ``os.listdir`` /
``os.scandir`` / ``os.walk`` / ``glob.glob`` / ``open`` whose argument is the
sentinel, with the stack that raised it. Each must come from a
``_lib_input`` frame: a tool that lists the directory itself (a ``glob``, an
``iterdir``, an ``os.listdir`` of its own) is red, whatever it is spelled.
A run that never lists the sentinel from Python is a dead row, also red.

The rows are not written here: they are the declarations themselves (read
off the parsers by G1's walk), so a new ``add_input`` flag is audited
without an edit to this file. Files read UNDER the directory are not judged
yet (``read_bytes``, #3445).

#3445: a flag declared with ``add_input(decode=...)`` names a FILE whose
content the tool reads; ``_lib_input.document`` is its one reading. Its row
is run the same way, the locked {F} replaced by a sentinel FILE holding
content the declaration accepts (so the run goes on past the read): an
``open`` of the sentinel from any frame but ``_lib_input``'s is a fault — a
tool's own ``open`` / ``json.load`` / ``yaml.safe_load`` of it, a second
reading — and a run that never opens it from ``_lib_input`` is a dead row.

⚠️ Not covered: a listing of the directory a tool reaches by another
spelling of the path that ``realpath`` does not equate (a bind mount), and
a listing by a child process (da-guard is Go; it is meant to list).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
AUDIT_DIR = Path(__file__).resolve().parent / "_input_audit"
LISTER = "_lib_input.py:"


def _tables():
    """test_caller_paths.py's declared-input rows (and its OPS_SITES)."""
    spec = importlib.util.spec_from_file_location(
        "_caller_paths_for_audit", Path(__file__).with_name("test_caller_paths.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def declared():
    rows = _tables()._declared_inputs()
    assert rows, "no add_input declaration was read off a parser — the audit measured nothing"
    return rows


def _subst(tmp: Path) -> dict:
    conf = REPO / "tests" / "golden" / "fixtures" / "flat" / "conf.d"
    (tmp / "silences.json").write_text("[]\n", encoding="utf-8")
    return {"{CONF}": str(conf),
            "{MAP}": str(REPO / "components" / "threshold-exporter" / "config" / "conf.d"
                         / "examples"),
            "{RP}": str(REPO / "rule-packs"), "{WF}": str(REPO / "tests" / "dx" / "fixtures"
                                                         / "waveform"),
            "{BOX}": str(tmp)}


def _run_audited(script: str, argv: list[str], sentinel: Path, tmp: Path) -> list[dict]:
    log = tmp / "audit.jsonl"
    env = dict(os.environ, DA_INPUT_AUDIT_LOG=str(log), DA_INPUT_AUDIT_SENTINEL=str(sentinel),
               DA_LANG="en",
               PYTHONPATH=os.pathsep.join([str(AUDIT_DIR), os.environ.get("PYTHONPATH", "")]))
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "tools" / "ops" / script),
                           *argv], cwd=tmp, env=env, capture_output=True, text=True,
                          encoding="utf-8", timeout=300, check=False, stdin=subprocess.DEVNULL)
    if not log.exists():
        pytest.fail(f"{script}: the audit hook logged nothing — it did not load, or the row is "
                    f"dead (rc {proc.returncode}): {(proc.stdout + proc.stderr)[-2000:]}")
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.mark.usefixtures("da_guard_env", "da_read_env")
def test_a_declared_input_directory_is_listed_only_by_lib_input(declared, tmp_path):
    faults = []
    for n, (rid, script, _flag, _spec, argv, _reason) in enumerate(declared):
        case = tmp_path / f"row-{n}"
        case.mkdir()
        sentinel = case / "sentinel"
        sentinel.mkdir()
        (sentinel / "a.json").write_text('{"schema_version": "1.0"}\n', encoding="utf-8")
        subst = {**_subst(case), "{D}": str(sentinel)}
        args = []
        for a in argv:
            for k, v in subst.items():
                a = a.replace(k, v)
            args.append(a)
        events = _run_audited(script, args, sentinel, case)
        listed = [e for e in events if e["ev"] != "open"]
        if not listed:
            faults.append(f"{rid}: the run never listed {sentinel} from Python — dead row")
        faults += [f"{rid}: {e['ev']}({e['path']}) outside _lib_input: {e['stack'][:6]}"
                   for e in events if not any(f.startswith(LISTER) for f in e["stack"])]
    assert not faults, "\n".join(faults)


def _hooked(code: str, sentinel: Path, tmp: Path) -> list[dict]:
    log = tmp / "probe.jsonl"
    env = dict(os.environ, DA_INPUT_AUDIT_LOG=str(log), DA_INPUT_AUDIT_SENTINEL=str(sentinel),
               PYTHONPATH=os.pathsep.join([str(AUDIT_DIR), str(REPO / "scripts" / "tools")]))
    subprocess.run([sys.executable, "-c", code, str(sentinel)], env=env, check=True,
                   timeout=60, cwd=tmp)
    return ([json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            if log.exists() else [])


@pytest.mark.parametrize("code, ev", [
    ("import sys, pathlib; sorted(pathlib.Path(sys.argv[1]).glob('*.json'))", "os.scandir"),
    ("import sys, pathlib; list(pathlib.Path(sys.argv[1]).iterdir())", "os.scandir"),
    ("import sys, os; os.listdir(sys.argv[1])", "os.listdir"),
    ("import sys, os; list(os.walk(sys.argv[1]))", "os.walk"),
    ("import sys, glob; glob.glob(sys.argv[1] + '/*.json')", "glob.glob"),
])
def test_the_hook_sees_every_way_to_list_the_sentinel(tmp_path, code, ev):
    """The hook is not a vocabulary: each spelling of a listing is an event on
    the path, from a frame that is not `_lib_input`'s."""
    sentinel = tmp_path / "s"
    sentinel.mkdir()
    events = _hooked(code, sentinel, tmp_path)
    assert any(e["ev"] == ev for e in events), events
    assert not any(f.startswith(LISTER) for e in events for f in e["stack"]), events


def test_a_listing_through_lib_input_carries_its_frame(tmp_path):
    sentinel = tmp_path / "s"
    sentinel.mkdir()
    (tmp_path / "other").mkdir()
    events = _hooked("import sys, os, _lib_input; _lib_input._list(sys.argv[1]); "
                     "os.listdir(os.path.join(sys.argv[1], '..', 'other'))", sentinel, tmp_path)
    assert [e["ev"] for e in events] == ["os.listdir"], events  # only the sentinel's
    assert any(f.startswith(LISTER) for f in events[0]["stack"]), events


@pytest.mark.parametrize("call", [
    "cr.files_to_read([sys.argv[1]])",
    "cr.RULE_PACKS.named_in(sys.argv[1])",
    "cr.RULE_PACKS.files_in(sys.argv[1])",
])
def test_lib_consumer_read_lists_through_lib_input(tmp_path, call):
    """#3446: `_walk_yaml`, `RuleSource.named_in` and `_declared_in` were three
    listings of their own; each is `_lib_input._list` now."""
    sentinel = tmp_path / "s"
    sentinel.mkdir()
    (sentinel / "rule-pack-x.yaml").write_text("groups: []\n", encoding="utf-8")
    events = _hooked(f"import sys, _lib_consumer_read as cr; {call}", sentinel, tmp_path)
    assert events, "the call never listed the sentinel — the probe measured nothing"
    assert all(any(f.startswith(LISTER) for f in e["stack"]) for e in events), events


#: A content each declared format and top level accepts (the first type of
#: a tuple), so the audited run reads past the sentinel.
_ACCEPTED = {("json", dict): "{}\n", ("json", list): "[]\n", ("yaml", dict): "{}\n",
             ("yaml", list): "[]\n", ("text", str): ""}


@pytest.fixture(scope="module")
def declared_documents():
    rows = _tables()._declared_inputs(documents=True)
    assert rows, "no add_input(decode=) declaration was read off a parser — nothing audited"
    return rows


@pytest.mark.usefixtures("da_guard_env", "da_read_env")
def test_a_declared_input_file_is_read_only_by_lib_input(declared_documents, tmp_path):
    faults = []
    for n, (rid, script, _flag, spec, argv, _reason) in enumerate(declared_documents):
        doc = spec.decode
        top = doc.top[0] if isinstance(doc.top, tuple) else doc.top
        case = tmp_path / f"doc-{n}"
        case.mkdir()
        sentinel = case / "sentinel"
        sentinel.write_text(_ACCEPTED[(doc.fmt, top)], encoding="utf-8")
        subst = {**_subst(case), "{F}": str(sentinel)}
        args = []
        for a in argv:
            for k, v in subst.items():
                a = a.replace(k, v)
            args.append(a)
        events = _run_audited(script, args, sentinel, case)
        if not any(any(f.startswith(LISTER) for f in e["stack"]) for e in events
                   if e["ev"] == "open"):
            faults.append(f"{rid}: the run never opened {sentinel} from _lib_input — dead row")
        faults += [f"{rid}: {e['ev']}({e['path']}) outside _lib_input: {e['stack'][:6]}"
                   for e in events if not any(f.startswith(LISTER) for f in e["stack"])]
    assert not faults, "\n".join(faults)


def test_an_open_of_the_sentinel_outside_lib_input_is_seen(tmp_path):
    """The audit's fault is real: a tool's own `open` of the file is an
    event from a frame that is not `_lib_input`'s; `document`'s is one."""
    sentinel = tmp_path / "s.json"
    sentinel.write_text("{}", encoding="utf-8")
    events = _hooked("import sys, json; json.load(open(sys.argv[1], encoding='utf-8'))",
                     sentinel, tmp_path)
    assert [e["ev"] for e in events] == ["open"], events
    assert not any(f.startswith(LISTER) for f in events[0]["stack"]), events
    (tmp_path / "lib").mkdir()  # a log of its own
    events = _hooked("import sys, argparse, _lib_input as li; p = argparse.ArgumentParser(); "
                     "li.add_input(p, '--f', decode=li.Document('json', dict)); "
                     "li.document(p.parse_args(['--f', sys.argv[1]]), 'f', lang='en')",
                     sentinel, tmp_path / "lib")
    assert events and all(any(f.startswith(LISTER) for f in e["stack"]) for e in events), events
