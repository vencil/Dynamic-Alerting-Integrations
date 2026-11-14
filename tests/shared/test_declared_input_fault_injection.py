"""#3724: a declared input that vanishes after it was judged is never read as success.

`_lib_input` is the one judgement of a caller's input (#3446, #3721): once it
said the path is there, a tool that looks again — a stat, a listing, an open,
a child process it starts — can see it gone. This gate makes it go: every
argument a da-tools parser declares with ``_lib_input.add_input`` (read off
the parsers by G1's walk, ``test_caller_paths._declared_inputs``) is run
under a start-up hook (``_fault_injection/sitecustomize.py``) that counts
each look at the input after `_lib_input`'s audit event says it is judged
(``JUDGED_EVENT``), and every ``subprocess.Popen`` after it. With N such
points in a run, the run is repeated for K = 1..N with the input removed at
the K-th point — and, where the K-th point looks at a file under it, with
that file removed instead.

Each injected run must be one of (the oracle; SQLite's I/O-error loop has
the same two answers — an error, or the right result):

(a) a refusal: rc 2, no crash (no traceback, no
    ``_lib_exitcodes.CRASH_LINE_PREFIX``), nothing on stdout the baseline
    does not print (an rc-2 partial answer is an answer), and a line the baseline does not
    have that names the flag, the path, or a path under it (a Go reader's own
    words naming the file it could not open count: it reads it itself);
(b) the baseline's answer: the same rc, and stdout, stderr and every file
    the run wrote equal to the baseline's — lines that differ between two
    baselines (a timestamp, a temporary path) are masked first, as Diffy
    masks the noise two runs of the same code disagree on.

Anything else is a fault: an answer from less input than was given (rc 0 /
1 with another output, a degraded row, a notice on stderr), a crash, or a
refusal that names neither. A refusal blaming something else (a da-guard
"binary failed" hint) still names the path through da-guard's words; that
wording is not judged here.

A row proves nothing unless it reads its input, so it is red when: its
baseline is rc 2 or a crash; the run never raised the judged event for the input (or
raised it twice in one process: one judgement, #3721); or
the baseline with the input's MINIMAL content answers exactly as with its
live content (the answer does not depend on the input). The content and
argv a row needs to be live are below (``LIVE``), keyed by the row's id; a
key that is no row is red. A point must also land where the baseline's did
(same site, same path), and must fire.

A row whose run takes no look at its input after the judgement (a file
`_lib_input.document` read whole) has no point: it passes on being live,
and its count of points is recorded (``record_property("points", N)``) so
"measured, nothing to inject" stays told apart from "injected, held".

⚠️ Not covered: a child process that is not Python is seen only at its start
(``Popen``), not at its own reads — and where the test stands in for it (the
``kubectl`` stub reads no file), a fault at that point only checks the
Python around it; a path reached by another spelling
``realpath`` does not equate (a bind mount); a change of the content in
place (the input there, other bytes) — that is not a vanished input, and a
tool cannot tell it from the content it was given; a symlink the flag names
swapped, after the judgement, for another readable tree is that too.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
HOOK = pathlib.Path(__file__).resolve().parent / "_fault_injection"
sys.path.insert(0, str(REPO / "scripts" / "tools"))

import _lib_input  # noqa: E402
from _lib_exitcodes import CRASH_LINE_PREFIX  # noqa: E402

sys.path.insert(0, str(REPO / "tests"))
from _platform_fs import symlink_or_skip  # noqa: E402


def _tables():
    spec = importlib.util.spec_from_file_location(
        "_caller_paths_for_fault_injection", pathlib.Path(__file__).with_name(
            "test_caller_paths.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_T = _tables()
#: (kind, id, script, flag, InputSpec, argv): every declared input — "D" a
#: directory (`select=`), "F" a file whose content is read (`decode=`).
ROWS = ([("D", rid, s, f, spec, argv) for rid, s, f, spec, argv, _r in _T._declared_inputs()]
        + [("F", rid, s, f, spec, argv)
           for rid, s, f, spec, argv, _r in _T._declared_inputs(documents=True)])

_SERVED_CHAIN = REPO / "tests" / "golden" / "fixtures" / "served-chain" / "conf.d"
_RULE_PACK = REPO / "rule-packs" / "rule-pack-mariadb.yaml"


def _copy_tree(src: pathlib.Path):
    def build(dst: pathlib.Path) -> None:
        shutil.copytree(src, dst)
    return build


def _files(files: "dict[str, str]"):
    def build(dst: pathlib.Path) -> None:
        dst.mkdir()
        for name, text in files.items():
            (dst / name).parent.mkdir(parents=True, exist_ok=True)
            (dst / name).write_text(text, encoding="utf-8")
    return build


def _file(text: str):
    def build(dst: pathlib.Path) -> None:
        dst.write_text(text, encoding="utf-8")
    return build


#: A conf.d with nothing to serve: one platform file, no tenant.
_MIN_CONFD = _files({"_defaults.yaml": "defaults: {}\n"})


def _default_plan(kind: str, spec) -> dict:
    """The content a row gets unless ``LIVE`` says otherwise: (live, minimal).
    A file input's default is the same for both — on purpose: what a
    document must hold for its tool to answer is the tool's, so every such
    row is red (not content-sensitive) until ``LIVE`` gives it content."""
    if kind == "F":
        doc = spec.decode
        top = doc.top[0] if isinstance(doc.top, tuple) else doc.top
        if doc.fmt == "csv":
            text = ",".join(doc.columns) + "\n" + _T._valid_csv_row(doc) + "\n"
        else:
            text = {("json", dict): "{}\n", ("json", list): "[]\n", ("yaml", dict): "{}\n",
                    ("yaml", list): "[]\n", ("text", str): ""}[(doc.fmt, top)]
        return {"live": _file(text), "minimal": _file(text)}
    if spec.select == _lib_input.DA_GUARD_LISTS:
        return {"live": _copy_tree(_SERVED_CHAIN), "minimal": _MIN_CONFD}
    if type(spec.select).__name__ == "RuleSource":
        return {"live": _files({_RULE_PACK.name: _RULE_PACK.read_text(encoding="utf-8")}),
                "minimal": _files({"rule-pack-x.yaml": "groups: []\n"})}
    return {"live": _files({"a.json": '{"schema_version": "1.0"}\n'}),
            "minimal": _files({"a.json": "{}\n"})}


#: Row id → what makes the row live where the default does not: "live" /
#: "minimal" (content builders), "argv" (replacing the G1 row's, whose argv
#: is made to reach the path check, not to succeed — it names
#: ``http://127.0.0.1:9``, so a row that needs Prometheus says ``{PROM}``,
#: the test's own) and "prometheus" (``(path, params) -> the JSON document``
#: it answers; default: an empty vector).
LIVE: "dict[str, dict]" = {}


# ── group A: conf.d tools ──
_FIXTURES = HOOK / "fixtures"


#: Conf.d trees with a platform `_defaults.yaml` are written here, not kept as
#: files: a tracked `_defaults*` tree is a shipped-defaults root to the
#: threshold-reachability lint (`_DEFAULTS_CONFD_ROOTS`).
_ANALYZE_GAPS_CONFIG_DIR = {
    '_defaults.yaml': (
        'defaults:\n'
        '  custom_mysql_uptime: 9\n'
        '  custom_mysql_slow_queries: 5\n'
        '  custom_unknown_thing: 1\n'
        '  mysql_threads_running: 50\n'),
    'tenants.yaml': (
        'tenants:\n'
        '  tenant-a:\n'
        '    custom_mysql_uptime: 1\n'
        '    custom_unknown_thing: 3\n'
        '  tenant-b:\n'
        '    custom_mysql_slow_queries: 12\n'
        '    mysql_threads_running: 80\n'),
}

_MIGRATE_CREDENTIALS_CONFIG_DIR = {
    '_defaults.yaml': (
        'defaults:\n'
        '  cpu: 70\n'),
    'team/tenant-a.yaml': (
        "# tenant-a's own file: its receiver carries a credential written in clear\n"
        'tenants:\n'
        '  tenant-a:\n'
        '    cpu: "80"\n'
        '    _routing:\n'
        '      receiver:\n'
        '        type: slack\n'
        '        api_url: "https://hooks.slack.example.com/services/T0/B0/fixture-not-a-secret"\n'
        "        channel: '#alerts'\n"),
}

_TEST_NOTIFICATION_CONFIG_DIR = {
    '_defaults.yaml': (
        'defaults:\n'
        '  cpu: 70\n'),
    'tenants.yaml': (
        '# Two tenants with receivers to dry-run: a main receiver plus an override\n'
        '# for one, a lone webhook for the other.\n'
        'tenants:\n'
        '  tenant-a:\n'
        '    cpu: "80"\n'
        '    _routing:\n'
        '      receiver:\n'
        '        type: webhook\n'
        '        url: "https://main.example.com/alert"\n'
        '      overrides:\n'
        '        - alertname: HighCPU\n'
        '          receiver:\n'
        '            type: webhook\n'
        '            url: "https://cpu.example.com/alert"\n'
        '  tenant-b:\n'
        '    _routing:\n'
        '      receiver:\n'
        '        type: webhook\n'
        '        url: "https://hooks.example.com/alert"\n'),
}


def _targets(*jobs: "tuple[str, str]"):
    """A Prometheus answering ``/api/v1/targets`` with one active target per
    (job, instance); anything else is an empty vector."""
    def answer(path: str, params: dict) -> dict:
        if path == "/api/v1/targets":
            return {"status": "success", "data": {"activeTargets": [
                {"labels": {"job": job, "instance": inst, "namespace": "monitoring"},
                 "health": "up"} for job, inst in jobs], "droppedTargets": []}}
        return _empty_vector(path, params)
    return answer


def _week_of_samples(path: str, params: dict) -> dict:
    """A range selector's answer: one series, a sample every 5 minutes for a
    week (fixed timestamps, so two runs agree), values 10..20."""
    if path != "/api/v1/query" or "[" not in params.get("query", ""):
        return _empty_vector(path, params)
    t0 = 1_760_000_000
    values = [[t0 + 300 * i, str(10 + i % 11)] for i in range(7 * 288)]
    return {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {}, "values": values}]}}


LIVE.update({
    # The served-chain tree serves mysql_* and redis_* keys for its tenants:
    # with it those DB types are covered, with an empty conf.d blind spots.
    "blind-spot --config-dir": {
        "argv": ["--config-dir", "{D}", "--json-output", "--prometheus", "{PROM}"],
        "prometheus": _targets(("mysql-exporter", "mysql-0:9104"),
                               ("redis-exporter", "redis-0:9121")),
    },
    # custom_ keys a tenant is served: one with a Rule Pack match, one without.
    "analyze-gaps --config-dir": {
        "live": _files(_ANALYZE_GAPS_CONFIG_DIR),
        "argv": ["--config-dir", "{D}", "--json"],
    },
    # A tenant receiver with a credential in clear: the plan rewrites it.
    # The tenant file sits in a subdirectory: a file removed there leaves
    # the root's own stamp as it was (#3724 review: judged whole, not by root).
    "migrate-credentials --config-dir": {
        "live": _files(_MIGRATE_CREDENTIALS_CONFIG_DIR),
    },
    # Tenants with receivers: each is a dry-run row of the report.
    "test-notification --config-dir": {
        "live": _files(_TEST_NOTIFICATION_CONFIG_DIR),
    },
    # A tenant the served-chain tree serves (through three _defaults levels):
    # found with its merged hash; in an empty conf.d, not found.
    "tenant-verify --conf-d": {
        "argv": ["tenant-served-chain", "--conf-d", "{D}", "--json"],
    },
    # The served-chain tenant's own mysql_connections (60) against a week of
    # observed load far below it: a change worth retuning.
    "threshold-govern --config-dir": {
        "argv": ["--config-dir", "{D}", "--json", "--prometheus", "{PROM}"],
        "prometheus": _week_of_samples,
    },
    # The served-chain tenant's inheritance: three _defaults levels under it.
    "diagnose --config-dir": {
        "argv": ["tenant-served-chain", "--config-dir", "{D}", "--show-inheritance",
                 "--json", "--prometheus", "{PROM}"],
    },
    # --source-dir holds ConfigMap manifests (not a conf.d): two of them
    # with rule groups, against one with none.
    "migrate-to-operator --source-dir": {
        "live": _copy_tree(_FIXTURES / "migrate-to-operator-source-dir" / "live"),
        "minimal": _copy_tree(_FIXTURES / "migrate-to-operator-source-dir" / "minimal"),
    },
})

# ── group B: rule packs / dashboards / runtime ──
LIVE.update({
    # The expected PrometheusRule count is the number of packs selected:
    # three packs against one that holds no group.
    "operator-check --rule-packs-dir": {
        "live": _files({n: (REPO / "rule-packs" / n).read_text(encoding="utf-8")
                        for n in ("rule-pack-mariadb.yaml", "rule-pack-redis.yaml",
                                  "rule-pack-kafka.yaml")}),
    },
    # Declared ↔ loaded: the default G1 runtime (`[]`) is no /api/v1/rules
    # answer (rc 2) — the test's Prometheus serves a saved one.
    "runtime-audit --rule-packs-dir": {
        "argv": ["--rule-packs-dir", "{D}", "--prometheus", "{PROM}", "--json"],
        "prometheus": lambda path, _params: (
            json.loads((HOOK / "fixtures" / "runtime-audit-runtime-json" / "rules.json")
                       .read_text(encoding="utf-8"))
            if path == "/api/v1/rules" else _empty_vector(path, _params)),
    },
    "runtime-audit --runtime-json": {
        "live": _file((HOOK / "fixtures" / "runtime-audit-runtime-json" / "rules.json")
                      .read_text(encoding="utf-8")),
        "minimal": _file('{"status": "success", "data": {"groups": []}}\n'),
    },
    # Without --dry-run (kubectl is the test's stub) the import reports each
    # dashboard's title, read from its content.
    "grafana-import --dashboard-dir": {
        "argv": ["--dashboard-dir", "{D}", "--json"],
        "live": _files({n: (REPO / "k8s" / "03-monitoring" / n).read_text(encoding="utf-8")
                        for n in ("federation-audit-dashboard.json",
                                  "shadow-monitoring-dashboard.json",
                                  "tenant-log-query-dashboard.json")}),
        "minimal": _files({"dashboard.json": "{}\n"}),
    },
    "grafana-import --dashboard": {
        "argv": ["--dashboard", "{F}", "--json"],
        "live": _file((REPO / "k8s" / "03-monitoring" / "shadow-monitoring-dashboard.json")
                      .read_text(encoding="utf-8")),
    },
    # An amtool dump: a silence that matches, an over-broad one, an orphan
    # (a v1 alertname) and an expired one, against the shipped rule packs.
    "silencer-drift-check --silences-file": {
        "argv": ["--silences-file", "{F}", "--rule-pack-dir", "{RP}", "--json", "--ci"],
        "live": _file((HOOK / "fixtures" / "silencer-drift-check-silences-file"
                       / "silences.json").read_text(encoding="utf-8")),
    },
    # A policy stricter than the built-in one, over the shipped tenant packs.
    "lint --policy": {
        "argv": ["--policy", "{F}", "--rule-pack-dir", "{RP}", "--ci"],
        "live": _file((HOOK / "fixtures" / "lint-policy" / "policy.yaml")
                      .read_text(encoding="utf-8")),
    },
    # Tenants with custom_ thresholds: the shipped dictionary matches them
    # exactly; an empty one leaves only the prefix heuristic. (One file, not
    # a conf.d: a tracked `_defaults*` tree is a root the reachability gate
    # pins.)
    "analyze-gaps --metric-dictionary": {
        "argv": ["--tenant-config",
                 str(HOOK / "fixtures" / "analyze-gaps-metric-dictionary" / "tenants.yaml"),
                 "--metric-dictionary", "{F}", "--json"],
        "live": _file((REPO / "scripts" / "tools" / "metric-dictionary.yaml")
                      .read_text(encoding="utf-8")),
    },
})

# ── group C: documents ──
# Each row reads one file the tool decodes; live/minimal are the same kind of
# document (fixtures/<slug>/live.* and minimal.*), the minimal one all but
# empty. The Prometheus a row queries answers from its handler below.
LIVE.update({
    "alert-correlate --input": {
        "live": _file((HOOK / "fixtures" / "alert-correlate-input" / "live.json")
                      .read_text(encoding="utf-8")),
        "minimal": _file((HOOK / "fixtures" / "alert-correlate-input" / "minimal.json")
                         .read_text(encoding="utf-8")),
    },
    "fed-key --existing-jwks": {
        "live": _file((HOOK / "fixtures" / "fed-key-existing-jwks" / "live.json")
                      .read_text(encoding="utf-8")),
        "minimal": _file((HOOK / "fixtures" / "fed-key-existing-jwks" / "minimal.json")
                         .read_text(encoding="utf-8")),
    },
    "cutover --readiness-json": {
        "live": _file((HOOK / "fixtures" / "cutover-readiness-json" / "live.json")
                      .read_text(encoding="utf-8")),
        "minimal": _file((HOOK / "fixtures" / "cutover-readiness-json" / "minimal.json")
                         .read_text(encoding="utf-8")),
    },
    "shadow-verify --mapping": {
        "live": _file((HOOK / "fixtures" / "shadow-verify-mapping" / "live.yaml")
                      .read_text(encoding="utf-8")),
        "minimal": _file((HOOK / "fixtures" / "shadow-verify-mapping" / "minimal.yaml")
                         .read_text(encoding="utf-8")),
        "argv": ["preflight", "--mapping", "{F}", "--json", "--prometheus", "{PROM}",
                 "--alertmanager", "{PROM}"],
        # Prometheus: healthy, 3 tenant:* names, 6 shadow series; Alertmanager
        # (the same server): a config with the migration_status matcher.
        "prometheus": lambda path, params: (
            "Prometheus Server is Healthy." if path == "/-/healthy" else
            {"config": {"original": "route:\n  receiver: default\n  routes:\n"
                                    "  - receiver: shadow-null\n    matchers:\n"
                                    "    - migration_status=\"shadow\"\n"}}
            if path.endswith("/api/v2/status") else
            {"status": "success", "data": {"resultType": "vector", "result": [
                {"metric": {}, "value": [1767225600, "3" if "tenant:" in
                                         params.get("query", "") else "6"]}]}}),
    },
    "shadow-verify --readiness-json": {
        "live": _file((HOOK / "fixtures" / "shadow-verify-readiness-json" / "live.json")
                      .read_text(encoding="utf-8")),
        "minimal": _file((HOOK / "fixtures" / "shadow-verify-readiness-json" / "minimal.json")
                         .read_text(encoding="utf-8")),
    },
    "shadow-verify --report-csv": {
        "live": _file((HOOK / "fixtures" / "shadow-verify-report-csv" / "live.csv")
                      .read_text(encoding="utf-8")),
        "minimal": _file((HOOK / "fixtures" / "shadow-verify-report-csv" / "minimal.csv")
                         .read_text(encoding="utf-8")),
        "argv": ["runtime", "--report-csv", "{F}", "--json", "--prometheus", "{PROM}"],
    },
    "scaffold --from-onboard": {
        "live": _file((HOOK / "fixtures" / "scaffold-from-onboard" / "live.json")
                      .read_text(encoding="utf-8")),
        "minimal": _file((HOOK / "fixtures" / "scaffold-from-onboard" / "minimal.json")
                         .read_text(encoding="utf-8")),
    },
    "validate --mapping": {
        "live": _file((HOOK / "fixtures" / "validate-mapping" / "live.yaml")
                      .read_text(encoding="utf-8")),
        "minimal": _file((HOOK / "fixtures" / "validate-mapping" / "minimal.yaml")
                         .read_text(encoding="utf-8")),
        "argv": ["--mapping", "{F}", "--prometheus", "{PROM}"],
        # Every old and new query: the same two tenants, the same values.
        "prometheus": lambda _path, _params: {
            "status": "success", "data": {"resultType": "vector", "result": [
                {"metric": {"tenant": "tenant-x"}, "value": [1767225600, "42"]},
                {"metric": {"tenant": "tenant-y"}, "value": [1767225600, "17"]}]}},
    },
})

def _empty_vector(_path: str, _params: dict) -> dict:
    return {"status": "success", "data": {"resultType": "vector", "result": []}}


def _plan(kind, rid, spec, argv) -> dict:
    return {**_default_plan(kind, spec), "argv": argv, "prometheus": _empty_vector,
            **LIVE.get(rid, {})}


class _Prometheus:
    """A Prometheus HTTP API on 127.0.0.1 for the length of a test: each
    request is answered by the row's ``prometheus`` handler."""

    def __init__(self, handler):
        import http.server
        import threading
        import urllib.parse

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self, params):
                url = urllib.parse.urlsplit(self.path)
                params = {**dict(urllib.parse.parse_qsl(url.query)), **params}
                body = json.dumps(handler(url.path, params)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 — http.server's name
                self._answer({})

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                self._answer(dict(urllib.parse.parse_qsl(self.rfile.read(n).decode())))

            def log_message(self, *_args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_every_live_override_is_a_row():
    rows = {r[1] for r in ROWS}
    assert not set(LIVE) - rows, f"LIVE names no row: {sorted(set(LIVE) - rows)}"


# ── running a row ────────────────────────────────────────────────────────

def _env(case: pathlib.Path, meta: pathlib.Path, input_path: pathlib.Path, k: int,
         mode: str) -> dict:
    env = {key: v for key, v in os.environ.items()
           if key != "PYTHONPATH" and not key.startswith(("COVERAGE_", "DA_FI_"))}
    env.update(DA_LANG="en", HOME=str(case / "home"), PYTHONPATH=str(HOOK),
               PYTHONDONTWRITEBYTECODE="1", DA_FI_INPUT=str(input_path),
               DA_FI_LOG=str(meta / "log.jsonl"), DA_FI_COUNT=str(meta / "count"),
               DA_FI_K=str(k), DA_FI_MODE=mode,
               PATH=f"{case.parent / 'bin'}{os.pathsep}{os.environ['PATH']}")
    return env


def _stub_kubectl(where: pathlib.Path) -> None:
    stub = where / "bin" / "kubectl"
    if stub.exists():
        return
    stub.parent.mkdir(parents=True)
    # `get <kind> <name> --ignore-not-found`: nothing; a list: no items.
    stub.write_text("#!/bin/sh\nfor a in \"$@\"; do [ \"$a\" = --ignore-not-found ] && "
                    "exit 0; done\necho '{\"items\": []}'\n", encoding="utf-8")
    stub.chmod(0o755)


class Run:
    def __init__(self, rc, stdout, stderr, files, events):
        self.rc, self.stdout, self.stderr = rc, stdout, stderr
        self.files, self.events = files, events

    @property
    def points(self):
        return [e for e in self.events if e["ev"] != "judged"]

    @property
    def judged(self):
        return [e for e in self.events if e["ev"] == "judged"]


def _run(row_dir: pathlib.Path, n: int, kind: str, plan: dict, script: str, content: str,
         k: int = 0, mode: str = "input") -> Run:
    """One run in a fresh case directory: the input at ``{CASE}/in``,
    ``{BOX}`` = ``{CASE}/box``, the working directory and HOME under it.
    Output and files have the case directory spelled ``{CASE}``."""
    _stub_kubectl(row_dir)
    case = row_dir / f"run-{n}"
    meta = row_dir / f"meta-{n}"
    for d in (case / "box", case / "home", meta):
        d.mkdir(parents=True)
    input_path = case / "in"
    plan[content](input_path)
    (case / "box" / "silences.json").write_text("[]\n", encoding="utf-8")
    subst = {"{D}": str(input_path), "{F}": str(input_path), "{BOX}": str(case / "box"),
             "{PROM}": plan.get("prom_url", "http://127.0.0.1:9"),
             "{CONF}": str(REPO / "tests" / "golden" / "fixtures" / "flat" / "conf.d"),
             "{MAP}": str(REPO / "components" / "threshold-exporter" / "config" / "conf.d"
                          / "examples"),
             "{RP}": str(REPO / "rule-packs"),
             "{WF}": str(REPO / "tests" / "dx" / "fixtures" / "waveform")}
    args = []
    for a in plan["argv"]:
        for key, v in subst.items():
            a = a.replace(key, v)
        args.append(a)
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "tools" / script), *args],
                          cwd=case, env=_env(case, meta, input_path, k, mode),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=300, check=False, stdin=subprocess.DEVNULL)
    spelled = str(case)

    def norm(text: str) -> str:
        return text.replace(spelled, "{CASE}")

    files = {}
    for p in sorted(case.rglob("*")):
        if p.is_file() and not p.is_relative_to(input_path) and p.name != "silences.json":
            files[str(p.relative_to(case))] = norm(p.read_bytes().decode("utf-8", "replace"))
    log = meta / "log.jsonl"
    events = ([json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
              if log.exists() else [])
    for e in events:
        e["path"] = norm(e["path"])
    return Run(proc.returncode, norm(proc.stdout), norm(proc.stderr), files, events)


# ── the oracle ───────────────────────────────────────────────────────────

def _masked_equal(b0: str, b1: str, got: str) -> bool:
    """*got* equals *b0* on every line *b0* and *b1* agree on (Diffy's noise
    mask). Baselines of different lengths: *got* must equal one of them."""
    a, b, c = b0.splitlines(), b1.splitlines(), got.splitlines()
    if len(a) != len(b):
        return c in (a, b)
    return len(c) == len(a) and all(x == z for x, y, z in zip(a, b, c) if x == y)


def same_answer(b0: Run, b1: Run, got: Run) -> bool:
    """Oracle (b): the baseline's rc, streams and files, noise masked."""
    if got.rc != b0.rc or set(got.files) != set(b0.files):
        return False
    return (_masked_equal(b0.stdout, b1.stdout, got.stdout)
            and _masked_equal(b0.stderr, b1.stderr, got.stderr)
            and all(_masked_equal(b0.files[f], b1.files.get(f, b0.files[f]), got.files[f])
                    for f in b0.files))


def crashed(run: Run) -> bool:
    text = run.stdout + run.stderr
    return ("Traceback (most recent call last)" in text or CRASH_LINE_PREFIX in text
            or run.rc not in (0, 1, 2))


def named_refusal(base: Run, got: Run, flag: str, names: "list[str]") -> bool:
    """Oracle (a): rc 2, no crash, and a line the baseline does not have
    naming *flag* or one of *names* (the input; the file removed)."""
    if got.rc != 2 or crashed(got):
        return False
    # A refusal answers nothing: no stdout line the baseline lacks (an rc-2
    # partial answer is an answer made from less input — what this gate
    # forbids; text a tool prints before it reads is allowed).
    if set(got.stdout.splitlines()) - set(base.stdout.splitlines()):
        return False
    seen = set((base.stdout + base.stderr).splitlines())
    # A name counts whole (`{CASE}/in` is not named by `{CASE}/input2`, nor
    # `--dir` by `--dir-x`); a path under the input counts — the first read
    # of a vanished input is of a file under it (a Go reader names that file).
    named = re.compile("|".join([re.escape(flag) + r"(?![\w-])"]
                                + [re.escape(n) + r"(?![\w.-])" for n in names]))
    return any(line not in seen and named.search(line)
               for line in (got.stdout + got.stderr).splitlines())


def _key(event: dict) -> tuple:
    return (event["where"], event["site"], event["path"])


# ── the gate ─────────────────────────────────────────────────────────────

@pytest.mark.usefixtures("da_guard_env", "da_read_env")
@pytest.mark.parametrize("row", ROWS, ids=[r[1] for r in ROWS])
def test_a_vanished_input_is_never_read_as_success(row, tmp_path, record_property):
    kind, rid, script, flag, spec, argv = row
    plan = _plan(kind, rid, spec, argv)
    prometheus = _Prometheus(plan["prometheus"])
    plan["prom_url"] = prometheus.url
    try:
        _gate_row(tmp_path, kind, rid, script, flag, plan, record_property)
    finally:
        prometheus.close()


def _gate_row(tmp_path, kind, rid, script, flag, plan, record_property):
    seq = iter(range(10_000))
    b0 = _run(tmp_path, next(seq), kind, plan, script, "live")
    # Over a second apart: a timestamp printed to the second differs between
    # the two baselines, so the noise mask takes it (#3724 review — a clock
    # stood still by replacing `datetime` changed what the tools compute).
    time.sleep(1.1)
    b1 = _run(tmp_path, next(seq), kind, plan, script, "live")
    bm = _run(tmp_path, next(seq), kind, plan, script, "minimal")
    tail = (b0.stdout + b0.stderr)[-1500:]
    assert b0.rc != 2 and b1.rc != 2, (
        f"{rid}: the baseline is rc {b0.rc} — the row never gets past its input (dead "
        f"row; give it live content/argv in LIVE):\n{tail}")
    assert not crashed(b0) and not crashed(b1), (
        f"{rid}: a baseline crashed — the row proves nothing (dead row):\n{tail}")
    # The tool's own process (its parent is this test): a child that judges
    # the path for itself (the route generator) does not judge it for the tool.
    mine = [e for e in b0.judged if e["path"] == "{CASE}/in" and e["ppid"] == os.getpid()]
    assert mine, (
        f"{rid}: the tool never raised {_lib_input.JUDGED_EVENT} for {flag} — its judgement "
        f"is not `_lib_input`'s (selected / document / record_judgement):\n{tail}")
    twice = [k for k in {(e["pid"], e["flag"]) for e in mine}
             if sum((e["pid"], e["flag"]) == k for e in mine) > 1]
    assert not twice, (f"{rid}: {flag} was judged more than once in one process — a second "
                       "judgement can disagree with the first (#3721)")
    assert not same_answer(b0, b1, bm), (
        f"{rid}: the run answers the same with the input's minimal content as with its live "
        "content — what it reads is not measured (give it content its answer depends on, LIVE)")
    keys0, keys1 = [_key(e) for e in b0.points], [_key(e) for e in b1.points]
    assert keys0 == keys1, f"{rid}: the two baselines look at the input differently"
    record_property("points", len(keys0))
    plans = [(k, "input") for k in range(1, len(keys0) + 1)]
    plans += [(e["k"], "file") for e in b0.points
              if e["where"] == "child" and not e["path"].endswith("/in")
              and (tmp_path / "run-0" / e["path"].removeprefix("{CASE}/")).is_file()]
    faults = []
    for k, mode in plans:
        got = _run(tmp_path, next(seq), kind, plan, script, "live", k=k, mode=mode)
        fired = [e for e in got.points if e["fired"]]
        at = b0.points[k - 1]
        where = f"{rid}: K={k} ({mode}) at {at['site']} {at['ev']}({at['path']})"
        if len(fired) != 1:
            faults.append(f"{where}: the fault did not fire ({len(fired)} fired)")
            continue
        if _key(fired[0]) != _key(at):
            faults.append(f"{where}: the {k}-th point of this run is "
                          f"{fired[0]['site']} {fired[0]['ev']}({fired[0]['path']})")
            continue
        names = ["{CASE}/in"] + ([at["path"]] if mode == "file" else [])
        if named_refusal(b0, got, flag, names) or same_answer(b0, b1, got):
            continue
        what = "crashed" if crashed(got) else (
            "refused naming neither the flag nor the path" if got.rc == 2
            else "answered from less input than it was given")
        faults.append(f"{where}: rc {got.rc}, {what}:\n"
                      f"{(got.stdout + got.stderr)[-600:]}")
    assert not faults, "\n".join(faults)


# ── the gate's own instruments ───────────────────────────────────────────

def _hooked(tmp_path, code: str, *, k: int = 0, mode: str = "input",
            make=lambda p: p.mkdir()) -> "tuple[subprocess.CompletedProcess, list[dict]]":
    target = tmp_path / "in"
    make(target)
    meta = tmp_path / "meta"
    meta.mkdir()
    env = {key: v for key, v in os.environ.items() if not key.startswith("DA_FI_")}
    env.update(PYTHONPATH=os.pathsep.join([str(HOOK), str(REPO / "scripts" / "tools")]),
               DA_FI_INPUT=str(target), DA_FI_LOG=str(meta / "log.jsonl"),
               DA_FI_COUNT=str(meta / "count"), DA_FI_K=str(k), DA_FI_MODE=mode)
    proc = subprocess.run([sys.executable, "-c", code, str(target)], env=env, cwd=tmp_path,
                          capture_output=True, text=True, encoding="utf-8", timeout=60,
                          check=False)
    log = meta / "log.jsonl"
    return proc, ([json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
                  if log.exists() else [])


_JUDGE = "import sys, _lib_input as li; li.judged('--x', sys.argv[1]); "


@pytest.mark.parametrize("look", [
    "os.stat(p)", "os.lstat(p)", "os.path.exists(p)", "os.path.isdir(p)",
    "pathlib.Path(p).is_dir()", "pathlib.Path(p).exists()", "os.access(p, os.R_OK)",
    "os.listdir(p)", "list(os.scandir(p))", "list(pathlib.Path(p).iterdir())",
    "sorted(pathlib.Path(p).glob('*'))", "list(os.walk(p))", "glob.glob(p + '/*')",
    "open(p + '/f').close()", "subprocess.run(['true'])",
])
def test_every_look_after_the_judgement_is_a_point(tmp_path, look):
    """The hook is no vocabulary: each spelling of a look is a point."""
    def make(p):
        p.mkdir()
        (p / "f").write_text("x", encoding="utf-8")
    code = (_JUDGE + "import os, glob, pathlib, subprocess; p = sys.argv[1]; " + look)
    proc, events = _hooked(tmp_path, code, make=make)
    assert proc.returncode == 0, proc.stderr
    assert [e for e in events if e["ev"] != "judged"], events


def test_a_look_before_the_judgement_is_no_point(tmp_path):
    """The judgement's own looks are not counted: the line is the event."""
    proc, events = _hooked(tmp_path, "import os, sys; os.stat(sys.argv[1]); "
                           "os.listdir(sys.argv[1])")
    assert proc.returncode == 0, proc.stderr
    assert events == [], events


def test_the_judged_event_comes_from_selected(tmp_path):
    """`selected` raises it when its judgement is done; a selector's
    refusal (nothing selected, refused) raises none."""
    code = ("import sys, argparse, _lib_input as li, os; p = argparse.ArgumentParser(); "
            "li.add_input(p, '--d', select=li.TopLevel('*.json'), empty=li.EMPTY_ALLOWED); "
            "a = p.parse_args(['--d', sys.argv[1]]); li.selected(a, 'd', lang='en'); "
            "os.stat(sys.argv[1])")
    proc, events = _hooked(tmp_path, code)
    assert proc.returncode == 0, proc.stderr
    assert [e["ev"] for e in events] == ["judged", "os.stat"], events


def test_the_kth_point_removes_the_input(tmp_path):
    proc, events = _hooked(tmp_path, _JUDGE + "import os; p = sys.argv[1]; os.stat(p); "
                           "print(os.path.exists(p))", k=2)
    assert proc.stdout.strip() == "False", (proc.stdout, proc.stderr)
    assert [e["fired"] for e in events if e["ev"] != "judged"] == [False, True], events


def test_file_mode_removes_the_file_looked_at(tmp_path):
    def make(p):
        p.mkdir()
        (p / "f").write_text("x", encoding="utf-8")
        (p / "g").write_text("y", encoding="utf-8")
    code = _JUDGE + ("import os, contextlib; p = sys.argv[1]\n"
                     "with contextlib.suppress(FileNotFoundError): open(p + '/f').close()\n"
                     "print(sorted(os.listdir(p)))")
    proc, events = _hooked(tmp_path, code, k=1, mode="file", make=make)
    assert proc.stdout.strip() == "['g']", (proc.stdout, proc.stderr)


def test_the_count_is_shared_with_a_python_child(tmp_path):
    code = _JUDGE + ("import os, subprocess; os.stat(sys.argv[1]); "
                     "subprocess.run([sys.executable, '-c', 'import os, sys, _lib_input as li; "
                     "li.judged(\"--x\", sys.argv[1]); os.stat(sys.argv[1])', sys.argv[1]], "
                     "check=True)")
    proc, events = _hooked(tmp_path, code)
    assert proc.returncode == 0, proc.stderr
    points = [e for e in events if e["ev"] != "judged"]
    assert [e["k"] for e in points] == [1, 2, 3], points  # stat, Popen, the child's stat
    assert len({e["pid"] for e in points}) == 2, points


def test_a_threaded_look_is_counted(tmp_path):
    """The re-entry guard is per thread: a look from another thread while one
    is being logged is not dropped."""
    code = _JUDGE + ("import os, threading; p = sys.argv[1]; "
                     "ts = [threading.Thread(target=lambda: [os.stat(p) for _ in range(25)]) "
                     "for _ in range(8)]; [t.start() for t in ts]; [t.join() for t in ts]")
    proc, events = _hooked(tmp_path, code)
    assert proc.returncode == 0, proc.stderr
    assert len([e for e in events if e["ev"] != "judged"]) == 200


def _r(rc=0, out="", err="", files=None):
    return Run(rc, out, err, files or {}, [])


def test_the_oracle_masks_only_what_two_baselines_disagree_on():
    b0 = _r(0, "at: 1\nrows: 2\n")
    b1 = _r(0, "at: 9\nrows: 2\n")
    assert same_answer(b0, b1, _r(0, "at: 5\nrows: 2\n"))
    assert not same_answer(b0, b1, _r(0, "at: 1\nrows: 0\n"))
    assert not same_answer(b0, b1, _r(1, "at: 1\nrows: 2\n"))
    assert not same_answer(b0, b1, _r(0, "at: 1\nrows: 2\n", "NOTICE: not checked\n"))
    assert not same_answer(_r(files={"o": "a"}), _r(files={"o": "a"}), _r(files={"o": "b"}))


def test_a_refusal_must_name_and_not_crash():
    base = _r(0, "--config-dir x: 3 tenants\n")
    assert named_refusal(base, _r(2, "", "ERROR: --config-dir x: not found\n"), "--config-dir",
                         ["x"])
    assert not named_refusal(base, _r(2, "--config-dir x: 3 tenants\n"), "--config-dir", ["x"])
    assert not named_refusal(base, _r(2, "", "Traceback (most recent call last):\n x\n"),
                             "--config-dir", ["x"])
    assert not named_refusal(base, _r(2, "", f"{CRASH_LINE_PREFIX}KeyError): x\n"),
                             "--config-dir", ["x"])
    assert not named_refusal(base, _r(1, "", "ERROR: --config-dir x: not found\n"),
                             "--config-dir", ["x"])
    # A path under the input names it; a sibling path or another flag does not.
    assert named_refusal(base, _r(2, "", "ERROR: x/b.json: not found\n"), "--d", ["x"])
    assert not named_refusal(base, _r(2, "", "ERROR: x2/b.json: not found\n"), "--d", ["x"])
    assert not named_refusal(base, _r(2, "", "ERROR: --dir-other y\n"), "--dir", ["x"])
    # A partial answer naming the path is still an answer.
    assert not named_refusal(base, _r(2, '{"detail": "--config-dir x: gone"}\n'),
                             "--config-dir", ["x"])


# ── the refusal the fixes raise (`_lib_parse_cache.refuse_changed`) ─────

@pytest.fixture
def fresh_declarations(monkeypatch):
    import _lib_parse_cache as pc
    for name in ("_DECLARED", "_JUDGED_AS", "_GIVEN"):
        monkeypatch.setattr(pc, name, {})
    monkeypatch.setattr(pc._lib_input, "judged", lambda *_a: None)
    return pc


def test_a_flag_naming_a_link_removed_after_the_judgement_is_refused(tmp_path,
                                                                    fresh_declarations):
    """Keyed by the path as given as well as resolved: the link gone resolves
    to itself, and the judgement must still be found (#3724 review)."""
    from _lib_exitcodes import InputChanged
    pc = fresh_declarations
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree" / "t.yaml").write_text("tenants: {}\n", encoding="utf-8")
    link = tmp_path / "current"
    symlink_or_skip(tmp_path / "tree", link)
    pc.record_judgement(link, "--config-dir", "en", ["t.yaml"])
    link.unlink()
    assert pc.judged(link)
    with pytest.raises(InputChanged, match=f"^--config-dir {re.escape(str(link))}: changed"):
        pc.refuse_changed(link, "da-guard: gone")


def test_a_file_linked_out_of_the_tree_gone_is_refused_by_its_place(tmp_path,
                                                                    fresh_declarations):
    """A file reached through a link out of the tree is found under the tree
    by the link's own path, not only by where it resolves."""
    from _lib_exitcodes import InputChanged
    pc = fresh_declarations
    outside = tmp_path / "shared-defaults.yaml"
    outside.write_text("defaults: {}\n", encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()
    symlink_or_skip(outside, tree / "_defaults.yaml")
    pc.record_judgement(tree, "--config-dir", "en", ["_defaults.yaml"])
    outside.unlink()
    gone = FileNotFoundError(2, "No such file or directory")
    with pytest.raises(InputChanged):
        pc.refuse_gone(tree / "_defaults.yaml", gone)


def test_the_refusal_is_one_line_whatever_the_path_holds(tmp_path, fresh_declarations):
    from _lib_exitcodes import InputChanged
    pc = fresh_declarations
    root = tmp_path / "a\nERROR: forged\x1b[2J"
    root.mkdir()
    pc.record_judgement(root, "--config-dir", "en", [])
    root.rmdir()
    with pytest.raises(InputChanged) as exc:
        pc.refuse_changed(root, "x\n\x1b[31my")
    assert "\n" not in str(exc.value) and "\x1b" not in str(exc.value), str(exc.value)
