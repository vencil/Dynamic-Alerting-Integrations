"""A kubectl that is on PATH but cannot be started, at every entry that runs one (#3539).

`_lib_kubectl.kubectl_run` is the one place that starts kubectl, for reads
and writes; whether it started at all is decided there (`_lib_engine`). Each
entry below is run as its CLI against a real file named `kubectl` at the
front of PATH that the kernel will not execute:

* `eacces` — no execute permission (mode 0644);
* `enoexec` — executable, but bytes the kernel cannot run;
* `no-interpreter` — a `#!` line naming an interpreter that does not exist;
* `absent` — no kubectl on PATH at all (the control: named before #3539 too).

Every cell is the contract's 2 (`_lib_exitcodes`: environment), one named
line, no traceback and no `unexpected internal error` — which is what the
first two were for every entry that started kubectl itself and caught only
`FileNotFoundError` / `TimeoutExpired`.

The second half is the part-way case of the write entries: a kubectl that
works for its first *n* calls and cannot be started after that. No such run
may say it succeeded.

Which modules may still start kubectl themselves is not listed here: that is
`tests/shared/test_engine_call_sites.py` (the AST gate).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _platform_fs import symlink_or_skip  # noqa: E402
from factories import make_routing_config, make_tenant_yaml

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and `#!` lines"),
    pytest.mark.usefixtures("da_guard_env", "da_read_env"),
]

REPO = Path(__file__).resolve().parents[2]
OPS = REPO / "scripts" / "tools" / "ops"
TENANT = "tenant-x"
_MIN_AM = "route:\n  receiver: r\nreceivers:\n- name: r\n"


# ── the inputs every entry is given ─────────────────────────────────────────

@pytest.fixture
def inputs(tmp_path):
    d = tmp_path / "in"
    (d / "confd").mkdir(parents=True)
    (d / "confd" / f"{TENANT}.yaml").write_text(
        make_tenant_yaml(TENANT, keys={"mysql_connections": "70"},
                         routing=make_routing_config(TENANT)), encoding="utf-8")
    (d / "rules").mkdir()
    (d / "rules" / "a.yaml").write_text("groups: []\n", encoding="utf-8")
    (d / "dash.json").write_text(json.dumps({"title": "t", "panels": []}), encoding="utf-8")
    (d / "dashdir").mkdir()
    for name in ("a.json", "b.json", "c.json"):
        (d / "dashdir" / name).write_text(json.dumps({"title": name}), encoding="utf-8")
    (d / "ready.json").write_text(json.dumps({
        "ready": True, "timestamp": "2026-01-01T00:00:00Z",
        "convergence_percentage": 100, "converged_count": 1, "total_pairs": 1,
    }), encoding="utf-8")
    (d / "state-keys.txt").write_text("synth-0000.yaml\n", encoding="utf-8")
    return d


def _entries(inputs: Path, work: Path) -> dict[str, list[str]]:
    """entry → its argv (after the interpreter). Each reaches kubectl on its
    first cluster step with these inputs."""
    cutover = [str(OPS / "cutover_tenant.py"), "--readiness-json", str(inputs / "ready.json"),
               "--tenant", TENANT, "--prometheus", "http://127.0.0.1:9"]
    grafana = [str(OPS / "grafana_import.py"), "--dashboard", str(inputs / "dash.json")]
    bench = str(REPO / "scripts" / "ops" / "bench_synth_tenants.py")
    return {
        "grafana-import": grafana,
        "grafana-import --json": [*grafana, "--json"],
        "grafana-import --verify": [str(OPS / "grafana_import.py"), "--verify"],
        "grafana-import --dashboard-dir": [str(OPS / "grafana_import.py"), "--dashboard-dir",
                                           str(inputs / "dashdir")],
        "grafana-import --dashboard-dir --json": [
            str(OPS / "grafana_import.py"), "--dashboard-dir", str(inputs / "dashdir"), "--json"],
        "cutover-tenant": cutover,
        "cutover-tenant --json-output": [*cutover, "--json-output"],
        "drift-detect --mode operator": [str(OPS / "drift_detect.py"), "--mode", "operator",
                                         "--dirs", str(inputs / "rules")],
        "configmap-assemble": [str(REPO / "scripts" / "ops" / "configmap_assemble.py"),
                               "--config-dir", str(inputs / "confd"),
                               "--output", str(work / "threshold-config.yaml")],
        "bench-synth-tenants inject": [bench, "inject", "--tenants", "2",
                                       "--state-file", str(work / "state.txt")],
        "bench-synth-tenants cleanup": [bench, "cleanup",
                                        "--state-file", str(inputs / "state-keys.txt")],
        "generate-alertmanager-routes --apply": [
            str(OPS / "generate_alertmanager_routes.py"), "--config-dir",
            str(inputs / "confd"), "--apply", "--yes"],
    }


ENTRIES = list(_entries(Path("in"), Path("work")))


#: What the fake kubectls below run (`#!/bin/sh` is an absolute path; these
#: are looked up on PATH). Nothing else: the tools are started with
#: `sys.executable`, the Go readers by `$DA_*_BINARY`.
_SHIM_TOOLS = ("cat", "chmod")


@pytest.fixture
def toolbox(tmp_path):
    """A directory holding symlinks to `_SHIM_TOOLS` and nothing else — the
    only directory on PATH besides the test's own kubectl directory."""
    box = tmp_path / "toolbox"
    box.mkdir()
    for tool in _SHIM_TOOLS:
        real = shutil.which(tool)
        assert real, f"`{tool}` is not on PATH: the fake kubectl cannot run"
        symlink_or_skip(real, box / tool)
    return box


def _run(argv: list[str], bindir: Path, toolbox: Path) -> subprocess.CompletedProcess:
    """Run a tool with a PATH that can resolve `kubectl` to *bindir*'s file
    or to nothing.

    ⛔ These entries WRITE (`kubectl apply`, `patch`, `delete`). A PATH that
    also held the interpreter's directory or `/usr/bin` would, on a machine
    with a real kubectl there, send those writes to whatever cluster the
    current kube-context names in the columns where *bindir*'s kubectl is
    absent or not executable. So PATH is *bindir* plus a directory of
    symlinks to the two tools the fakes need — and that is measured before
    the tool is started, not assumed: any other `kubectl` fails the cell."""
    env = dict(os.environ)
    env.pop("PYTHONUNBUFFERED", None)
    env.pop("ALLOW_SAMPLE_CONFDIR", None)
    env["PATH"] = os.pathsep.join([str(bindir), str(toolbox)])
    found = shutil.which("kubectl", path=env["PATH"])
    assert found is None or Path(found).parent == bindir, (
        f"PATH resolves kubectl to {found}, outside the test's own directory — "
        "the tool was NOT started")
    assert sorted(p.name for p in toolbox.iterdir()) == sorted(_SHIM_TOOLS), (
        "the toolbox holds something else — the tool was NOT started")
    return subprocess.run([sys.executable, "-X", "utf8", *argv], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=env,
                          stdin=subprocess.DEVNULL, timeout=300)


# ── kubectl cannot be started at all ────────────────────────────────────────

_SCENARIOS = {
    "eacces": (0o644, "#!/bin/sh\necho {}\n", "is on PATH but is not executable"),
    "enoexec": (0o755, "\x00\x01not a program\n", "Exec format error"),
    "no-interpreter": (0o755, "#!/no/such/interp\n", "No such file or directory"),
    "absent": (None, None, "kubectl is not available"),
}


def _stage(bindir: Path, scenario: str) -> str:
    mode, body, why = _SCENARIOS[scenario]
    bindir.mkdir()
    if mode is not None:
        exe = bindir / "kubectl"
        exe.write_text(body, encoding="utf-8")
        exe.chmod(mode)
    return why


@pytest.mark.parametrize("scenario", list(_SCENARIOS))
@pytest.mark.parametrize("entry", ENTRIES)
def test_a_kubectl_that_cannot_be_started_is_a_named_exit_2(tmp_path, inputs, toolbox,
                                                            entry, scenario):
    why = _stage(tmp_path / "bin", scenario)
    work = tmp_path / "work"
    work.mkdir()
    r = _run(_entries(inputs, work)[entry], tmp_path / "bin", toolbox)
    said = r.stdout + r.stderr
    assert r.returncode == 2, f"rc={r.returncode}\n{said[-1500:]}"
    assert "Traceback" not in said, said[-1500:]
    assert "unexpected internal error" not in said, said[-1500:]
    # stdout too: `grafana-import` reports an apply that could not be started
    # as a row of its table.
    assert why in said and "kubectl" in said, said[-1500:]
    assert not list(work.iterdir()), (
        "nothing may be written when kubectl never ran", list(work.iterdir()))


def test_a_kubectl_outside_the_tests_directory_fails_the_cell_before_the_tool_starts(
        tmp_path, toolbox):
    """The guard in `_run`, shown to fire: a `kubectl` reachable from
    anywhere but the test's own directory stops the cell, and the tool is not
    started (its argv here would fail differently — no such file)."""
    stray = toolbox / "kubectl"
    stray.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stray.chmod(0o755)
    (tmp_path / "bin").mkdir()
    # each guard by its own words: the other one firing must not stand in
    with pytest.raises(AssertionError, match="PATH resolves kubectl to .* outside the test's"):
        _run([str(tmp_path / "no-such-tool.py")], tmp_path / "bin", toolbox)
    stray.unlink()
    symlink_or_skip("/bin/sh", toolbox / "git")
    with pytest.raises(AssertionError, match="the toolbox holds something else"):
        _run([str(tmp_path / "no-such-tool.py")], tmp_path / "bin", toolbox)


# ── part-way: kubectl works for its first n calls, then cannot be started ───

_DYING_KUBECTL = """#!/bin/sh
n=$(cat "{d}/count"); n=$((n+1)); echo $n > "{d}/count"
echo "$1 $2" >> "{d}/calls"
case "$1" in
  get) cat "{d}/get.json" ;;
  create) printf 'apiVersion: v1\\nkind: ConfigMap\\nmetadata:\\n  name: x\\n' ;;
  *) echo "$1 ok" ;;
esac
if [ $n -ge {n} ]; then
  {die}
fi
exit 0
"""
_DIE = {
    "eacces": 'chmod 0644 "$0"',
    "enoexec": "printf '\\000\\001junk\\n' > \"$0\"",
}

#: (entry, calls that succeed first, what `kubectl get` answers, the calls
#: made, words a success would print). The run must exit 2 without them.
_PART_WAY = [
    ("grafana-import", 2, {"data": {}}, ["create configmap", "apply -f"],
     ["Overall: PASS"]),
    ("grafana-import --json", 2, {"data": {}}, ["create configmap", "apply -f"],
     ['"status": "pass",\n  "actions"']),
    # the first dashboard of the directory is applied and labelled; the
    # second one's `create` is the call that cannot be started
    ("grafana-import --dashboard-dir", 3, {"data": {}},
     ["create configmap", "apply -f", "label configmap"], ["Overall: PASS"]),
    ("grafana-import --dashboard-dir --json", 3, {"data": {}},
     ["create configmap", "apply -f", "label configmap"], ['"status": "pass",\n  "actions"']),
    ("cutover-tenant", 1, {"data": {}}, ["delete job"],
     ["Cutover completed successfully"]),
    ("cutover-tenant --json-output", 3, {"data": {}},
     ["delete job", "delete configmap", "label configmap"],
     ["Cutover completed successfully", '"success": true']),
    ("bench-synth-tenants inject", 1, {"data": {}}, ["get configmap"], ["Injected"]),
    ("bench-synth-tenants cleanup", 1, {"data": {}}, ["patch configmap"], ["Removed"]),
    ("generate-alertmanager-routes --apply", 1, {"data": {"alertmanager.yml": _MIN_AM}},
     ["get configmap"], ["updated"]),
    ("generate-alertmanager-routes --apply", 2, {"data": {"alertmanager.yml": _MIN_AM}},
     ["get configmap", "create configmap"], ["updated"]),
]


@pytest.mark.parametrize("how", list(_DIE))
@pytest.mark.parametrize("entry, alive, get, calls, success", _PART_WAY,
                         ids=[f"{e}-after-{n}" for e, n, *_ in _PART_WAY])
def test_a_write_entry_never_reports_success_once_kubectl_stops_starting(
        tmp_path, inputs, toolbox, entry, alive, get, calls, success, how):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "count").write_text("0\n", encoding="utf-8")
    (tmp_path / "calls").write_text("", encoding="utf-8")
    (tmp_path / "get.json").write_text(json.dumps(get), encoding="utf-8")
    exe = bindir / "kubectl"
    exe.write_text(_DYING_KUBECTL.format(d=tmp_path, n=alive, die=_DIE[how]),
                   encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()

    r = _run(_entries(inputs, work)[entry], bindir, toolbox)
    said = r.stdout + r.stderr
    made = (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
    # the fixture did what it says: exactly the first calls ran, no later one
    assert made == calls, (made, said[-1500:])
    assert r.returncode == 2, f"rc={r.returncode}\n{said[-1500:]}"
    assert "Traceback" not in said and "unexpected internal error" not in said, said[-1500:]
    assert "cannot run kubectl" in said, said[-1500:]
    for words in success:
        assert words not in said, (words, said[-1500:])


def _dying_kubectl(tmp_path, alive, how="eacces"):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "count").write_text("0\n", encoding="utf-8")
    (tmp_path / "calls").write_text("", encoding="utf-8")
    (tmp_path / "get.json").write_text("{}", encoding="utf-8")
    exe = bindir / "kubectl"
    exe.write_text(_DYING_KUBECTL.format(d=tmp_path, n=alive, die=_DIE[how]),
                   encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    return bindir


@pytest.mark.parametrize("how", list(_DIE))
def test_a_dashboard_dir_keeps_the_rows_of_what_it_applied(tmp_path, inputs, toolbox, how):
    """Review of #3539: over a --dashboard-dir, the first dashboard applied
    and labelled and the second one's `kubectl create` not startable ended in
    one ERROR line with stdout EMPTY — the rows of what is in the cluster
    dropped. They stay (text and --json), the dashboard that failed gets its
    row, the ones after it are named as not attempted, rc 2."""
    bindir = _dying_kubectl(tmp_path, 3, how)
    entries = _entries(inputs, tmp_path)
    r = _run(entries["grafana-import --dashboard-dir"], bindir, toolbox)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "ConfigMap grafana-a applied" in r.stdout, r.stdout
    assert "Label grafana_dashboard=1 applied to grafana-a" in r.stdout, r.stdout
    assert "`kubectl create configmap grafana-b`: cannot run kubectl" in r.stdout, r.stdout
    assert "1 dashboard(s) after grafana-b were not attempted" in r.stdout, r.stdout
    assert "Overall: FAIL" in r.stdout

    (tmp_path / "bin" / "kubectl").unlink()
    (tmp_path / "bin").rmdir()
    bindir = _dying_kubectl(tmp_path, 3, how)
    r = _run(entries["grafana-import --dashboard-dir --json"], bindir, toolbox)
    assert r.returncode == 2, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert doc["status"] == "fail"
    assert [(a["action"], a["status"]) for a in doc["actions"]] == [
        ("create_configmap", "pass"), ("label_configmap", "pass"), ("create_configmap", "fail")]
    assert "were not attempted" in doc["actions"][2]["detail"]
    # c.json was not attempted: kubectl was started three times, for a.json
    assert (tmp_path / "calls").read_text(encoding="utf-8").splitlines() == [
        "create configmap", "apply -f", "label configmap"]


def test_grafana_import_says_the_configmap_is_applied_but_not_labelled(tmp_path, inputs,
                                                                      toolbox):
    """The one part-way case with something left in the cluster that the
    operator has to finish: the row that passed stays in the report."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "count").write_text("0\n", encoding="utf-8")
    (tmp_path / "calls").write_text("", encoding="utf-8")
    (tmp_path / "get.json").write_text("{}", encoding="utf-8")
    exe = bindir / "kubectl"
    exe.write_text(_DYING_KUBECTL.format(d=tmp_path, n=2, die=_DIE["eacces"]),
                   encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    r = _run(_entries(inputs, tmp_path)["grafana-import --json"], bindir, toolbox)
    assert r.returncode == 2, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert doc["status"] == "fail"
    assert [(a["action"], a["status"]) for a in doc["actions"]] == [
        ("create_configmap", "pass"), ("label_configmap", "fail")]
    assert "its label is NOT confirmed" in doc["actions"][1]["detail"]
