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

Repeats are not run (#4224). A child process that is handed nothing naming
the input — no argv element, cwd or environment value is the input or under
it, and it is not Python — is BLIND to it (the hook records ``blind`` on its
``subprocess.Popen`` point): removing the input just before a blind child
starts is the same fault as removing it at the next point. So in each run of
consecutive blind ``Popen`` points (``blind_blocks``) only the first is
injected; ``lint --policy`` hands its 82 Go readers stdin and repo paths, and
was 82 runs of one fault. The conclusion is cross-checked, not trusted: the
block's last point is injected too and must end the same way as its first.
⚠️ The cross-check sees a child that reads the input anyway (a path sent on
stdin) only when that changes how the run ends between the two ends of its
block.

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

#: Row id → further rows of the same declared input (#3902): the tool takes
#: another path over the same flag (deprecate's `--subtree`: the reader cut
#: from the root's document, no carrier check after the writes). Each is a
#: row ``"<id> [<name>]"`` and must have its own ``LIVE`` entry with an argv
#: (`test_every_variant_row_has_its_own_argv`) — else it would run the row
#: it is a variant of a second time.
VARIANTS = {"deprecate --config-dir": ("subtree",),
            # #3901: a gatherer's value is a file or a directory — the row
            # G1 hands a directory, and one more handing it a file.
            "lint paths": ("file",), "onboard --rule-files": ("file",),
            "silencer-drift-check --rule-source": ("file",)}
ROWS += [(kind, f"{rid} [{name}]", script, flag, spec, argv)
         for kind, rid, script, flag, spec, argv in list(ROWS) for name in VARIANTS.get(rid, ())]

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


#: A rule file Prometheus loads, with one alert (#4040's rows).
_PACK_ONE = ("groups:\n  - name: g\n    rules:\n      - alert: HighCPU\n"
             "        expr: cpu_usage > 0.9\n        for: 5m\n"
             "        labels: {severity: warning}\n")


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
    # Tenants declaring mariadb and redis (`_expected_exporters`, #4026): with
    # it those DB types are covered, with an empty conf.d blind spots.
    "blind-spot --config-dir": {
        "live": _files({
            "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
            "a.yaml": "tenants:\n  ta:\n    _expected_exporters: [mariadb]\n",
            "sub/b.yaml": "tenants:\n  tb:\n    _expected_exporters: [redis]\n"}),
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


def _migrated_rules_answer(legacy=False):
    """`/api/v1/rules`: the alerts of the mapping fixtures (#4227) loaded as
    migrated rules of tenant-x — and, *legacy*, as legacy rules beside them."""
    alerts = ("MySQLTooManyConnections", "MySQLSlowQueries")
    groups = [{"name": "migrated", "interval": 30, "rules": [
        {"name": a, "type": "alerting", "labels": {"tenant": "tenant-x", "da_migrated": "true"}}
        for a in alerts]}]
    if legacy:
        groups.append({"name": "legacy", "interval": 30, "rules": [
            {"name": a, "type": "alerting", "labels": {}} for a in alerts]})
    return {"status": "success", "data": {"groups": groups}}


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
        # Prometheus: healthy, the mapping's alerts loaded as migrated rules
        # of tenant-x (#4227); Alertmanager (the same server): a config with
        # the migrated-alerts shadow receiver (#4277).
        "prometheus": lambda path, params: (
            "Prometheus Server is Healthy." if path == "/-/healthy" else
            {"config": {"original": "route:\n  receiver: default\n  routes:\n"
                                    "  - receiver: migrated-alerts-shadow\n"
                                    "receivers:\n- name: default\n"
                                    "- name: migrated-alerts-shadow\n"}}
            if path.endswith("/api/v2/status") else
            _migrated_rules_answer()),
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
        # #4227: every alert loaded on both sides, every selector counting
        # two series, no ALERTS on either side — a clean round, rc 0.
        "prometheus": lambda path, params: (
            _migrated_rules_answer(legacy=True) if path == "/api/v1/rules" else
            {"status": "success", "data": {"resultType": "vector", "result": [
                {"metric": {}, "value": [1767225600, "2"]}]
                if params.get("query", "").startswith("count(") else []}}),
    },
})

# ── group D: documents a tool hands to its own reader (#3797) ──
# These were G1 `file` / `reader` lines; each is a declared document now. A
# row whose tool reads a conf.d beside the input builds it in {BOX}
# ("box": name -> builder; a `_defaults*` tree is written here, not kept).
_ROUTE_CONFD = {
    "tenants.yaml": (
        "tenants:\n"
        "  tenant-a:\n"
        "    _routing:\n"
        "      receiver:\n"
        "        type: webhook\n"
        "        url: https://hooks.example.com/main\n"
        "  tenant-b:\n"
        "    _routing:\n"
        "      receiver:\n"
        "        type: webhook\n"
        "        url: https://evil.example.org/hook\n"),
}
_ALLOW = "allowed_domains:\n  - hooks.example.com\n"
_ALLOW_NONE = "allowed_domains: []\n"
_BASE = ("route:\n  receiver: base-default\n  group_wait: 45s\n"
         "  routes:\n    - matchers: ['alertname=\"X\"']\n      receiver: stale\n"
         "receivers:\n  - name: base-default\n  - name: stale\n")
_BASE_MIN = "route:\n  receiver: base-default\n  group_wait: 7s\nreceivers:\n  - name: base-default\n"
_POLICY_CONFD = {
    "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
    "db-x.yaml": ("tenants:\n  db-x:\n    mysql_connections: '70'\n"
                  "    _metadata:\n      tier: production\n"),
    "db-y.yaml": "tenants:\n  db-y:\n    mysql_connections: '90'\n",
}
_POLICY_DSL = ("policies:\n  - name: metadata-required\n    target: _metadata\n"
               "    operator: required\n    severity: error\n")
_STATE_CONFD = {
    "_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
    "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: \"80\"\n",
    "tenant-b.yaml": "tenants:\n  tenant-b:\n    mysql_connections: \"90\"\n",
}


def _state(members: "list[str]") -> str:
    return json.dumps({"schema": "tenant-api.state-show/v1", "tenant_api_version": "x",
                       "records": [{"key": "_groups.yaml", "record": {"groups": {
                           "g-fin": {"label": "Finance", "members": members}}}}]}) + "\n"


_EK_CONFD = {
    "_defaults.yaml": ("defaults:\n  mysql_connection_usage_percent: 80\n"
                       "  mysql_slave_lag: 30\n  redis_memory: 70\n"),
    "tenant-a.yaml": ("tenants:\n  tenant-a:\n    mysql_conections: \"70\"\n"
                      "    mysql_slave_lagg: disable\n    redis_memory: disable\n"),
}
_AM = ("route:\n  receiver: default\n  routes:\n"
       "  - receiver: team-a\n    matchers: ['tenant=\"tenant-a\"']\n"
       "  - receiver: team-b\n    matchers: ['tenant=\"tenant-b\"']\n"
       "receivers:\n- name: default\n"
       "- name: team-a\n  webhook_configs: [{url: 'https://a.example.com/hook'}]\n"
       "- name: team-b\n  webhook_configs: [{url: 'https://b.example.com/hook'}]\n")
_AM_MIN = "route: {receiver: default}\nreceivers: [{name: default}]\n"
_SCRAPE = ("scrape_configs:\n- job_name: mysql\n  static_configs:\n"
           "  - targets: ['mysql-0:9104']\n    labels: {tenant: tenant-a}\n"
           "- job_name: redis\n  kubernetes_sd_configs: [{role: pod}]\n"
           "  relabel_configs:\n  - source_labels: [__meta_kubernetes_namespace]\n"
           "    target_label: tenant\n")
_SCRAPE_MIN = "scrape_configs:\n- job_name: other\n  static_configs: [{targets: ['h:1']}]\n"
_MOUNTS = {"schema": "da-tools.secret-mounts/v1", "mount_root": "/etc/alertmanager/secrets",
           "mounts": [{"name": "da-team-c", "keys": ["auth_password"]}], "other_files": []}

LIVE.update({
    # custom_ keys of one tenant file: one with a Rule Pack match, one without.
    "analyze-gaps --tenant-config": {
        "argv": ["--tenant-config", "{F}", "--json"],
        "live": _file("tenants:\n  tenant-a:\n    custom_mysql_uptime: 1\n"
                      "    custom_unknown_thing: 3\n"),
        "minimal": _file("tenants: {}\n"),
    },
    # A rule one tenant breaks (no _metadata), against no rule.
    "evaluate-policy --policy": {
        "argv": ["--config-dir", "{BOX}/conf.d", "--policy", "{F}", "--json", "--ci"],
        "box": {"conf.d": _files(_POLICY_CONFD)},
        "live": _file(_POLICY_DSL), "minimal": _file("policies: []\n"),
    },
    "validate-config --policy-dsl": {
        "argv": ["--config-dir", "{BOX}/conf.d", "--json", "--policy-dsl", "{F}"],
        "box": {"conf.d": _files(_POLICY_CONFD)},
        "live": _file(_POLICY_DSL), "minimal": _file("policies: []\n"),
    },
    # Expected verdicts, one of them wrong: the assertion fails on it.
    "explain-keys --assert": {
        "argv": ["--config-dir", "{BOX}/conf.d", "--assert", "{F}", "--json"],
        "box": {"conf.d": _files(_EK_CONFD)},
        "live": _file(json.dumps({"tenants": {"tenant-a": [
            {"key": "mysql_conections", "verdict": "read"},
            {"key": "redis_memory", "verdict": "read"}]}}) + "\n"),
        "minimal": _file('{"tenants": {}}\n'),
    },
    # The deployment policy: an allowlist that refuses tenant-b's receiver,
    # against none.
    "explain-route --policy": {
        "argv": ["--config-dir", "{BOX}/conf.d", "--json", "--policy", "{F}"],
        "box": {"conf.d": _files(_ROUTE_CONFD)},
        "live": _file(_ALLOW), "minimal": _file(_ALLOW_NONE),
    },
    "generate-routes --policy": {
        "argv": ["--config-dir", "{BOX}/conf.d", "--output-alertmanager",
                 "-o", "{BOX}/alertmanager.yml", "--policy", "{F}"],
        "box": {"conf.d": _files(_ROUTE_CONFD)},
        "live": _file(_ALLOW), "minimal": _file(_ALLOW_NONE),
    },
    "test-notification --policy": {
        "argv": ["--config-dir", "{BOX}/conf.d", "--dry-run", "--json", "--rate-limit", "0",
                 "--policy", "{F}"],
        "box": {"conf.d": _files(_ROUTE_CONFD)},
        "live": _file(_ALLOW), "minimal": _file(_ALLOW_NONE),
    },
    "validate-config --policy": {
        "argv": ["--config-dir", "{BOX}/conf.d", "--json", "--policy", "{F}"],
        "box": {"conf.d": _files(_ROUTE_CONFD)},
        "live": _file(_ALLOW), "minimal": _file(_ALLOW_NONE),
    },
    # A credential in clear on an email receiver: the plan's allowlist check
    # admits its host, or refuses it.
    "migrate-credentials --policy": {
        "argv": ["--config-dir", "{BOX}/conf.d", "--json", "--policy", "{F}"],
        "box": {"conf.d": _files({"team-c.yaml": (
            "tenants:\n  team-c:\n    _routing:\n      receiver:\n        type: email\n"
            "        to: [ops@example.com]\n        from: am@example.com\n"
            "        smarthost: smtp.example.com:587\n"
            "        auth_password: \"fixture-not-a-secret\"\n")})},
        "live": _file("allowed_domains: ['*.example.com']\n"),
        "minimal": _file("allowed_domains: ['other.example.org']\n"),
    },
    # The base the generated routing is merged into: its own route and timing.
    "explain-route --base-config": {
        "argv": ["--config-dir", "{BOX}/conf.d", "--tenant", "tenant-a", "--trace",
                 "--alertname", "X", "--json", "--base-config", "{F}"],
        "box": {"conf.d": _files(_ROUTE_CONFD)},
        "live": _file(_BASE), "minimal": _file(_BASE_MIN),
    },
    "generate-routes --base-config": {
        "argv": ["--config-dir", "{BOX}/conf.d", "--output-alertmanager",
                 "-o", "{BOX}/alertmanager.yml", "--base-config", "{F}"],
        "box": {"conf.d": _files(_ROUTE_CONFD)},
        "live": _file(_BASE), "minimal": _file(_BASE_MIN),
    },
    # A group with the tenant offboarded as a member: the pre-check names it.
    "offboard --state": {
        "argv": ["tenant-a", "--config-dir", "{BOX}/conf.d", "--state", "{F}"],
        "box": {"conf.d": _files(_STATE_CONFD)},
        "live": _file(_state(["tenant-a", "tenant-b"])), "minimal": _file(_state(["tenant-b"])),
    },
    # Two tenant routes, against none.
    "onboard --alertmanager-config": {
        "argv": ["--alertmanager-config", "{F}", "-o", "{BOX}/out", "--json", "--dry-run"],
        "live": _file(_AM), "minimal": _file(_AM_MIN),
    },
    "onboard --scrape-config": {
        "argv": ["--scrape-config", "{F}", "-o", "{BOX}/out", "--json", "--dry-run"],
        "live": _file(_SCRAPE), "minimal": _file(_SCRAPE_MIN),
    },
    # #4040: one alert, against none — the diff (or the migration) differs.
    "rule-pack-diff --from": {
        "argv": ["--from", "{F}", "--to", "{BOX}/to.yaml", "--json"],
        "box": {"to.yaml": _file(_PACK_ONE)},
        "live": _file(_PACK_ONE), "minimal": _file("groups: []\n"),
    },
    "rule-pack-diff --to": {
        "argv": ["--from", "{BOX}/from.yaml", "--to", "{F}", "--json"],
        "box": {"from.yaml": _file(_PACK_ONE)},
        "live": _file(_PACK_ONE), "minimal": _file("groups: []\n"),
    },
    # #4226: one alert migrated, against none.
    "migrate input_file": {
        "argv": ["{F}", "-o", "{BOX}/out", "--dry-run", "--tenant", "t1", "--owner", "team",
                 "--expiry", "2099-12-31", "--no-dictionary"],
        "live": _file(_PACK_ONE), "minimal": _file("groups: []\n"),
    },
    # The self-lint's policy: it admits the migrated rule, or denies its rate().
    "migrate --policy": {
        "argv": ["{BOX}/rules.yml", "-o", "{BOX}/out", "--dry-run", "--tenant", "t1",
                 "--owner", "team", "--expiry", "2099-12-31", "--no-dictionary",
                 "--policy", "{F}"],
        "box": {"rules.yml": _file("groups:\n  - name: g\n    rules:\n      - alert: HighErr\n"
                                   "        expr: rate(errors_total[5m]) > 1\n")},
        "live": _file("isolated_label: tenant\n"),
        "minimal": _file("isolated_label: tenant\ndenied_functions: [rate]\n"),
    },
    # One Secret mounted, against none: the list's row compares them.
    "operator-check --secret-mounts": {
        "argv": ["--rule-packs-dir", "{RP}", "--json", "--secret-mounts", "{F}"],
        "live": _file(json.dumps(_MOUNTS)), "minimal": _file(json.dumps({**_MOUNTS, "mounts": []})),
    },
})


def _chain_with(value: str):
    """The served-chain tree with tenant-served-chain's mysql_connections at *value*."""
    def build(dst: pathlib.Path) -> None:
        shutil.copytree(_SERVED_CHAIN, dst)
        (dst / "db" / "mariadb" / "tenants.yaml").write_text(
            f'tenants:\n  tenant-served-chain:\n    mysql_connections: "{value}"\n',
            encoding="utf-8")
    return build


#: A root with one subtree (`team/`) that still holds the metric — in the
#: subtree's carrier and in its tenant file — and the same tree without it.
_SUBTREE_CONFD = {
    "_defaults.yaml": "defaults:\n  mysql_connections: 70\n  some_metric: 5\n",
    "tenant-b.yaml": "tenants:\n  tenant-b:\n    mysql_connections: \"90\"\n",
    "team/_defaults.yaml": "defaults:\n  some_metric: \"7\"\n",
    "team/tenant-a.yaml": "tenants:\n  tenant-a:\n    some_metric: \"80\"\n",
}
_SUBTREE_CONFD_MIN = {
    "_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
    "tenant-b.yaml": "tenants:\n  tenant-b:\n    mysql_connections: \"90\"\n",
    "team/_defaults.yaml": "defaults:\n  mysql_connections: \"7\"\n",
    "team/tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: \"80\"\n",
}

_RULES = ("groups:\n- name: g\n  rules:\n  - alert: HighCPU\n    expr: up{tenant=\"t\"} == 0\n"
          "    labels: {severity: warning, tenant: t}\n")
_RULES_B = ("groups:\n- name: h\n  rules:\n  - alert: LowDisk\n    expr: up{tenant=\"t\"} == 1\n"
            "    labels: {severity: warning, tenant: t}\n")
#: A directory of two rule files (one in a subdirectory) against one; one
#: rule file against one holding no rule.
_RULE_DIR = {"live": _files({"a.yaml": _RULES, "sub/b.yaml": _RULES_B}),
             "minimal": _files({"a.yaml": _RULES})}
_RULE_FILE = {"live": _file(_RULES), "minimal": _file("groups: []\n")}
_ONBOARD_RULES = ["--rule-files", "{D}", "-o", "{BOX}/out", "--json", "--dry-run"]
_SILENCER_RULES = ["--silences-file", "{BOX}/silences.json", "--rule-source", "{D}", "--json"]

# ── #3901: a file or a directory per value (gatherers) ──
LIVE.update({
    "lint paths": {**_RULE_DIR},
    "lint paths [file]": {"argv": ["{F}"], **_RULE_FILE},
    "onboard --rule-files": {"argv": _ONBOARD_RULES, **_RULE_DIR},
    "onboard --rule-files [file]": {"argv": _ONBOARD_RULES, **_RULE_FILE},
    "silencer-drift-check --rule-source": {"argv": _SILENCER_RULES, **_RULE_DIR},
    "silencer-drift-check --rule-source [file]": {"argv": _SILENCER_RULES, **_RULE_FILE},
})

# ── #3902: the conf.d trees da-guard reads, declared ──
LIVE.update({
    # One threshold changed between the two trees, against none.
    "backtest --baseline": {
        "argv": ["--config-dir", "{BOX}/cur", "--baseline", "{D}", "--prometheus", "{PROM}",
                 "--json"],
        "box": {"cur": _copy_tree(_SERVED_CHAIN)},
        "live": _chain_with("50"), "minimal": _copy_tree(_SERVED_CHAIN),
    },
    "backtest --config-dir": {
        "argv": ["--config-dir", "{D}", "--baseline", "{BOX}/base", "--prometheus", "{PROM}",
                 "--json"],
        "box": {"base": _copy_tree(_SERVED_CHAIN)},
        "live": _chain_with("50"), "minimal": _copy_tree(_SERVED_CHAIN),
    },
    # The pre-check of a tenant that is there, with and without a neighbour.
    "offboard --config-dir": {
        "live": _files(_STATE_CONFD),
        "minimal": _files({k: v for k, v in _STATE_CONFD.items() if k != "tenant-b.yaml"}),
    },
    # The subtree plane: its carrier is read by `carrier_health_at`, and no
    # later read of it (the root plane's check after the writes) stands behind.
    "deprecate --config-dir [subtree]": {
        "argv": ["--config-dir", "{D}", "--subtree", "team", "some_metric"],
        "live": _files(_SUBTREE_CONFD), "minimal": _files(_SUBTREE_CONFD_MIN),
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


def test_every_variant_row_has_its_own_argv():
    rows = {r[1] for r in ROWS}
    for rid, names in VARIANTS.items():
        assert rid in rows, f"VARIANTS names no row: {rid}"
        for name in names:
            assert "argv" in LIVE.get(f"{rid} [{name}]", {}), (
                f"{rid} [{name}]: a variant without its own LIVE argv runs {rid} twice")


# ── running a row ────────────────────────────────────────────────────────

def _env(case: pathlib.Path, meta: pathlib.Path, input_path: pathlib.Path, k: int,
         mode: str, victim: str = "") -> dict:
    env = {key: v for key, v in os.environ.items()
           if key != "PYTHONPATH" and not key.startswith(("COVERAGE_", "DA_FI_"))}
    env.update(DA_LANG="en", HOME=str(case / "home"), PYTHONPATH=str(HOOK),
               PYTHONDONTWRITEBYTECODE="1", DA_FI_INPUT=str(input_path),
               DA_FI_LOG=str(meta / "log.jsonl"), DA_FI_COUNT=str(meta / "count"),
               DA_FI_K=str(k), DA_FI_MODE=mode, DA_FI_VICTIM=victim,
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
         k: int = 0, mode: str = "input", victim: str = "") -> Run:
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
    for name, build in plan.get("box", {}).items():  # #3797: trees beside the input
        build(case / "box" / name)
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
                          cwd=case, env=_env(case, meta, input_path, k, mode, victim),
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


def _verdict(b0: Run, b1: Run, got: Run, flag: str) -> str:
    """How an input-mode run ended, as the oracle sorts it."""
    if same_answer(b0, b1, got):
        return "the baseline's answer"
    if named_refusal(b0, got, flag, ["{CASE}/in"]):
        return "a refusal naming the input"
    return f"rc {got.rc}, neither"


def blind_blocks(points: "list[dict]") -> "list[tuple[int, int]]":
    """(first, last) point numbers of each maximal run of consecutive
    ``subprocess.Popen`` points whose child is blind to the input. PURE.

    A blind child (`_fault_injection/sitecustomize.py`: nothing it is handed
    names the input, and it is not Python) cannot tell the input is gone, so
    removing the input just before it starts is the same fault as removing
    it just before the next point: within a block, the injections at
    first+1..last repeat the one at first. One block of a single point skips
    nothing."""
    blocks, start = [], None
    for i, e in enumerate(points, 1):
        if e.get("ev") == "subprocess.Popen" and e.get("blind") is True:
            start = i if start is None else start
            continue
        if start is not None:
            blocks.append((start, i - 1))
            start = None
    if start is not None:
        blocks.append((start, len(points)))
    return blocks


def crashed(run: Run) -> bool:
    text = run.stdout + run.stderr
    return ("Traceback (most recent call last)" in text or CRASH_LINE_PREFIX in text
            or run.rc not in (0, 1, 2))


def named_refusal(base: Run, got: Run, flag: str, names: "list[str]") -> bool:
    """Oracle (a): rc 2, no crash, and a line the baseline does not have
    naming *flag* or one of *names* (the input; the file removed). A
    positional (*flag* with no leading ``-``, #3901) has no name a refusal
    could say — its dest is a plain word (``paths``) any line may hold — so
    only *names* count for it."""
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
    named = re.compile("|".join(([re.escape(flag) + r"(?![\w-])"] if flag.startswith("-") else [])
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
        _gate_row(tmp_path, kind, rid, script, flag, plan, record_property,
                  confd=kind == "D" and getattr(spec, "select", None) == _lib_input.DA_GUARD_LISTS)
    finally:
        prometheus.close()


def _gate_row(tmp_path, kind, rid, script, flag, plan, record_property, confd=False):
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
    assert [e.get("blind") for e in b0.points] == [e.get("blind") for e in b1.points], (
        f"{rid}: the two baselines hand their children different paths")
    record_property("points", len(keys0))
    blocks = blind_blocks(b0.points)
    repeats = {k for first, last in blocks for k in range(first + 1, last + 1)}
    record_property("skipped_as_repeats", len(repeats))
    plans = [(k, "input", "") for k in range(1, len(keys0) + 1) if k not in repeats]
    plans += [(e["k"], "file", "") for e in b0.points
              if e["where"] == "child" and not e["path"].endswith("/in")
              and (tmp_path / "run-0" / e["path"].removeprefix("{CASE}/")).is_file()]
    if confd:
        # #3965: a declared conf.d tree is walked again by da-guard (served-
        # values, effective, the route generator, …), which Python does not
        # look inside. Each file of the tree is removed at the judgement
        # itself and before every child process after it: the second walk
        # reads one file fewer, and the run must refuse naming the flag or
        # answer as with the whole tree.
        victims = sorted(p.relative_to(tmp_path / "run-0" / "in").as_posix()
                         for p in (tmp_path / "run-0" / "in").rglob("*")
                         if p.is_file() and not p.is_symlink())
        plans += [(0, "judged-victim", v) for v in victims]
        plans += [(e["k"], "victim", v) for e in b0.points if e["ev"] == "subprocess.Popen"
                  and e["k"] not in repeats for v in victims]
    faults = []
    first_of = {first: last for first, last in blocks if last > first}
    for k, mode, victim in plans:
        got = _run(tmp_path, next(seq), kind, plan, script, "live", k=k, mode=mode, victim=victim)
        if mode == "input" and k in first_of:
            # The skip rests on the children of the block being blind. Check
            # it rather than trust it: the block's LAST point must end the
            # same way as its first (the same answer, or a refusal naming
            # the input) — a child that read the input some way it was not
            # handed (stdin, a file it was pointed at) splits the two.
            last = first_of[k]
            other = _run(tmp_path, next(seq), kind, plan, script, "live", k=last)
            ends = [_verdict(b0, b1, run, flag) for run in (got, other)]
            if len([e for e in other.points if e["fired"]]) != 1:
                faults.append(f"{rid}: the cross-check at point {last} did not fire")
            elif ends[0] != ends[1]:
                faults.append(
                    f"{rid}: points {k}..{last} were skipped as repeats (children handed "
                    f"no path under the input), but removing the input at {k} ends "
                    f"'{ends[0]}' and at {last} ends '{ends[1]}' — a child in between "
                    "reads the input after all; its points are not repeats")
        if mode == "judged-victim":
            where = f"{rid}: {victim} removed at the judgement"
            if not any(e.get("fired") for e in got.judged):
                faults.append(f"{where}: the fault did not fire")
                continue
        else:
            fired = [e for e in got.points if e["fired"]]
            at = b0.points[k - 1]
            where = f"{rid}: K={k} ({mode}{' ' + victim if victim else ''}) at {at['site']} {at['ev']}({at['path']})"
            if len(fired) != 1:
                faults.append(f"{where}: the fault did not fire ({len(fired)} fired)")
                continue
            if _key(fired[0]) != _key(at):
                faults.append(f"{where}: the {k}-th point of this run is "
                              f"{fired[0]['site']} {fired[0]['ev']}({fired[0]['path']})")
                continue
        names = (["{CASE}/in"] + ([at["path"]] if mode == "file" else [])
                 + (["{CASE}/in/" + victim] if victim else []))
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


@pytest.mark.parametrize("child, blind", [
    ("subprocess.run(['true'])", True),                                  # handed nothing
    ("subprocess.run(['true'], input=b'x')", True),                      # stdin is not seen
    ("subprocess.run(['true', p])", False),                              # the input
    ("subprocess.run(['true', p + '/f'])", False),                       # a path under it
    ("subprocess.run(['true', '--dir=' + p])", False),                   # inside an argument
    ("subprocess.run(['true', os.path.basename(p)], cwd=os.path.dirname(p))", False),
    ("subprocess.run(['true'], cwd=p)", False),                          # runs inside it
    ("subprocess.run(['true'], env={'X': p, 'PATH': os.environ['PATH']})", False),
    ("subprocess.run([sys.executable, '-c', 'pass'])", False),          # Python looks itself
], ids=["nothing", "stdin", "argv-input", "argv-under", "argv-embedded", "argv-relative",
        "cwd-inside", "env", "python-child"])
def test_a_popen_point_says_whether_its_child_is_blind(tmp_path, child, blind):
    """`blind` is what lets the gate skip repeats, so every way a child can
    be handed the input must clear it (#4224)."""
    code = _JUDGE + "import os, subprocess; p = sys.argv[1]; " + child
    proc, events = _hooked(tmp_path, code)
    assert proc.returncode == 0, proc.stderr
    popen = [e for e in events if e["ev"] == "subprocess.Popen"]
    assert [e["blind"] for e in popen][:1] == [blind], events


@pytest.mark.parametrize("marks, expected", [
    ("", []),
    ("b", [(1, 1)]),
    ("bbb", [(1, 3)]),
    ("xbbxb", [(2, 3), (5, 5)]),
    ("bxpb", [(1, 1), (4, 4)]),     # a non-blind Popen ends a block
    ("xxx", []),
], ids=["empty", "one", "run", "two-runs", "split-by-popen", "none"])
def test_blind_blocks_are_maximal_runs_of_blind_popens(marks, expected):
    ev = {"b": {"ev": "subprocess.Popen", "blind": True},
          "p": {"ev": "subprocess.Popen", "blind": False},
          "x": {"ev": "os.stat"}}
    assert blind_blocks([ev[m] for m in marks]) == expected


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
    # A positional's dest names nothing (#3901): only the path counts.
    assert not named_refusal(base, _r(2, "", "ERROR: no paths given\n"), "paths", ["x"])
    assert named_refusal(base, _r(2, "", "ERROR: x: not found\n"), "paths", ["x"])
    # A path under the input names it; a sibling path or another flag does not.
    assert named_refusal(base, _r(2, "", "ERROR: x/b.json: not found\n"), "--d", ["x"])
    assert not named_refusal(base, _r(2, "", "ERROR: x2/b.json: not found\n"), "--d", ["x"])
    assert not named_refusal(base, _r(2, "", "ERROR: --dir-other y\n"), "--dir", ["x"])
    # A partial answer naming the path is still an answer.
    assert not named_refusal(base, _r(2, '{"detail": "--config-dir x: gone"}\n'),
                             "--config-dir", ["x"])


# ── the refusal the fixes raise (`_lib_parse_cache.refuse_changed`) ─────

def _identity(root) -> str:
    """The judgement's identity (#3965): the tree-mode document's config_hash."""
    import _lib_parse_tree
    return _lib_parse_tree.load(root).config_hash


@pytest.fixture
def fresh_declarations(monkeypatch):
    import _lib_parse_cache as pc
    for name in ("_DECLARED", "_JUDGED_AS", "_GIVEN"):
        monkeypatch.setattr(pc, name, {})
    monkeypatch.setattr(pc._lib_input, "judged", lambda *_a: None)
    # Judgements are handed to child processes through the environment
    # (#3965): set it so monkeypatch puts back what was there.
    monkeypatch.setenv(pc.JUDGED_TREES_ENV, "")
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
    pc.record_judgement(link, "--config-dir", "en", _identity(link))
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
    pc.record_judgement(tree, "--config-dir", "en", _identity(tree))
    outside.unlink()
    gone = FileNotFoundError(2, "No such file or directory")
    with pytest.raises(InputChanged):
        pc.refuse_gone(tree / "_defaults.yaml", gone)


def test_the_refusal_is_one_line_whatever_the_path_holds(tmp_path, fresh_declarations):
    from _lib_exitcodes import InputChanged
    pc = fresh_declarations
    root = tmp_path / "a\nERROR: forged\x1b[2J"
    root.mkdir()
    pc.record_judgement(root, "--config-dir", "en", _identity(root))
    root.rmdir()
    with pytest.raises(InputChanged) as exc:
        pc.refuse_changed(root, "x\n\x1b[31my")
    assert "\n" not in str(exc.value) and "\x1b" not in str(exc.value), str(exc.value)


def test_a_directory_of_a_declared_tree_is_refused_as_that_tree(tmp_path, fresh_declarations):
    """#3902: a directory below a declared tree, read on its own (deprecate's
    `--subtree`: `da-guard key-refs` runs on it), is that tree — its refusal
    after the tree changed names the flag; with the tree as it was judged the
    refusal is the directory's own (no raise). Looked up by the exact root
    only, a vanished subtree was a "not checked" run that went on."""
    from _lib_exitcodes import InputChanged
    pc = fresh_declarations
    root = tmp_path / "conf.d"
    (root / "team").mkdir(parents=True)
    (root / "team" / "t.yaml").write_text("tenants: {}\n", encoding="utf-8")
    pc.record_judgement(root, "--config-dir", "en", _identity(root))
    pc.refuse_changed(root / "team", "da-guard key-refs: no .yaml files")  # as judged
    assert not pc.judged(root / "team")  # the judgement itself stays the root's
    (root / "team" / "t.yaml").unlink()
    with pytest.raises(InputChanged, match=f"^--config-dir {re.escape(str(root))}: changed"):
        pc.refuse_changed(root / "team", "da-guard key-refs: no .yaml files")


def test_a_file_under_two_declared_trees_is_the_inner_ones(tmp_path, fresh_declarations):
    """#3902: with one declared tree inside another (backtest's --baseline
    below its --config-dir), a file gone is named by the NEAREST tree — by
    `refuse_gone` as by `refuse_changed`, whatever order they were declared in."""
    from _lib_exitcodes import InputChanged
    pc = fresh_declarations
    outer, inner = tmp_path / "conf.d", tmp_path / "conf.d" / "base"
    inner.mkdir(parents=True)
    (inner / "t.yaml").write_text("tenants: {}\n", encoding="utf-8")
    gone = FileNotFoundError(2, "No such file or directory")
    for order in ((outer, inner), (inner, outer)):
        for name in ("_DECLARED", "_JUDGED_AS", "_GIVEN"):
            getattr(pc, name).clear()
        (inner / "t.yaml").write_text("tenants: {}\n", encoding="utf-8")
        for tree in order:
            pc.record_judgement(tree, "--baseline" if tree == inner else "--config-dir", "en",
                                _identity(tree))
        (inner / "t.yaml").unlink()
        for refuse in (lambda: pc.refuse_gone(inner / "t.yaml", gone),
                       lambda: pc.refuse_changed(inner, "gone")):
            with pytest.raises(InputChanged, match="^--baseline "):
                refuse()




# ── #3965: every later walk of a judged tree is compared with its judgement ──

def _judged_tree(tmp_path, pc):
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 70\n", encoding="utf-8")
    (root / "a.yaml").write_text("tenants:\n  ta:\n    mysql_connections: \"80\"\n", encoding="utf-8")
    pc.record_judgement(root, "--config-dir", "en", _identity(root))
    return root


@pytest.mark.parametrize("read", [
    pytest.param(lambda pc, root: pc.ConfdReader(root).tree(), id="parse-tree read again"),
    pytest.param(lambda pc, root: pc.judge_now(root), id="judge_now"),
    pytest.param(lambda pc, root: __import__("_lib_tenant_values").load_guard_report(root),
                 id="the guard report"),
    pytest.param(lambda pc, root: __import__("_lib_tenant_values").load_served_values(root),
                 id="served-values"),
])
def test_a_later_walk_of_another_tree_is_the_flags_refusal(tmp_path, fresh_declarations, read):
    """A da-guard document of a judged tree whose walk read another tree
    (a file added since the judgement) is the refusal naming the flag —
    whatever its exit code; the same tree reads as before."""
    from _lib_exitcodes import InputChanged
    pc = fresh_declarations
    root = _judged_tree(tmp_path, pc)
    read(pc, root)  # the tree as judged: no refusal
    (root / "b.yaml").write_text("tenants:\n  tb:\n    mysql_connections: \"81\"\n", encoding="utf-8")
    with pytest.raises(InputChanged, match=f"^--config-dir {re.escape(str(root))}: changed after it was judged"):
        read(pc, root)


def test_a_child_run_reads_the_tree_its_parent_judged(tmp_path, fresh_declarations):
    """#3965 review: a tool's child process judges the tree again in its own
    process (config-diff's route generator). The parent's judgement is handed
    down: a child that reads another tree refuses naming the flag, never
    answers for its parent from a tree the parent did not judge."""
    pc = fresh_declarations
    root = _judged_tree(tmp_path, pc)
    argv = [sys.executable, str(REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"),
            "--config-dir", str(root), "--output-alertmanager", "--dry-run"]

    def child():
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300, check=False, env=dict(os.environ))
    same = child()
    assert same.returncode == 0, same.stderr[-800:]
    (root / "b.yaml").write_text("tenants:\n  tb:\n    mysql_connections: \"81\"\n", encoding="utf-8")
    got = child()
    assert got.returncode == 2 and "--config-dir" in got.stderr \
        and "judged another tree" in got.stderr, (got.returncode, got.stderr[-800:])


#: A child run that judges the declared trees named on its command line —
#: `--a`, and `--b` when given — as a tool does (declared_trees): rc 0, or
#: rc 2 with the refusal's line.
_CHILD_JUDGES = """
import argparse, sys
sys.path.insert(0, {tools!r})
import _lib_input, _lib_parse_cache
from _lib_exitcodes import InputChanged
p = argparse.ArgumentParser()
_lib_input.add_input(p, "--a", select=_lib_input.DA_GUARD_LISTS)
_lib_input.add_input(p, "--b", select=_lib_input.DA_GUARD_LISTS)
args = p.parse_args(sys.argv[1:])
try:
    for dest in ("a", "b"):
        if getattr(args, dest) is not None:
            _lib_parse_cache.declared_trees(args, dest, lang="en")
except InputChanged as exc:
    print(exc, file=sys.stderr)
    sys.exit(2)
"""


def _child_judges(*argv: str) -> "subprocess.CompletedProcess":
    code = _CHILD_JUDGES.format(tools=str(REPO / "scripts" / "tools"))
    return subprocess.run([sys.executable, "-c", code, *argv], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300, check=False,
                          env=dict(os.environ))


def _a_tree(root, tenant: str):
    root.mkdir(parents=True)
    (root / f"{tenant}.yaml").write_text(f"tenants:\n  {tenant}:\n    mysql_connections: \"80\"\n",
                                         encoding="utf-8")
    return root


def test_a_child_compares_every_tree_it_judges_with_its_parent(tmp_path, fresh_declarations):
    """#3965 (CodeRabbit): a child judging two trees compares the second
    with its parent's judgement as it does the first — its own first
    judgement does not drop what the parent handed down."""
    pc = fresh_declarations
    a, b = _a_tree(tmp_path / "a", "ta"), _a_tree(tmp_path / "b", "tb")
    for flag, root in (("--a", a), ("--b", b)):
        pc.record_judgement(root, flag, "en", _identity(root))
    same = _child_judges("--a", str(a), "--b", str(b))
    assert same.returncode == 0, same.stderr[-600:]
    (b / "more.yaml").write_text("tenants:\n  tc: {}\n", encoding="utf-8")
    got = _child_judges("--a", str(a), "--b", str(b))
    assert got.returncode == 2 and f"--b {b}" in got.stderr \
        and "judged another tree" in got.stderr, (got.returncode, got.stderr[-600:])


def test_a_child_reaching_a_tree_through_a_retargeted_link_is_refused(tmp_path, fresh_declarations):
    """#3965 (CodeRabbit): the parent judged the tree through a link; the
    link retargeted before the child runs leads it to another tree, found by
    the link's own path and refused."""
    pc = fresh_declarations
    first, other = _a_tree(tmp_path / "first", "ta"), _a_tree(tmp_path / "other", "tz")
    link = tmp_path / "current"
    symlink_or_skip(first, link)
    pc.record_judgement(link, "--a", "en", _identity(link))
    assert _child_judges("--a", str(link)).returncode == 0
    link.unlink()
    symlink_or_skip(other, link)
    got = _child_judges("--a", str(link))
    assert got.returncode == 2 and "judged another tree" in got.stderr, (got.returncode, got.stderr[-600:])

def test_a_second_run_in_one_process_judges_its_own_tree(tmp_path, fresh_declarations):
    """#3965 review: judgements are process state. A second run of a tool in
    the same process, after an edit between the runs, judges the tree as it
    is then — never compared with the first run's tree."""
    import argparse
    pc = fresh_declarations
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "a.yaml").write_text("tenants:\n  ta:\n    mysql_connections: \"80\"\n", encoding="utf-8")

    def run_once():
        parser = argparse.ArgumentParser()
        _lib_input.add_input(parser, "--config-dir", select=_lib_input.DA_GUARD_LISTS)
        args = parser.parse_args(["--config-dir", str(root)])
        pc.declared_trees(args, "config_dir", lang="en")
        pc.ConfdReader(root).tree()  # a later read of it in the same run
    run_once()
    (root / "b.yaml").write_text("tenants:\n  tb:\n    mysql_connections: \"81\"\n", encoding="utf-8")
    run_once()


def test_a_tree_da_guard_now_refuses_is_a_changed_tree(tmp_path, fresh_declarations):
    """#3965 review: a tree parse-tree judged, and refuses after (a name that
    is not valid UTF-8 added), changed — never "the binary, not the tree"."""
    from _lib_exitcodes import InputChanged
    pc = fresh_declarations
    root = _judged_tree(tmp_path, pc)
    try:
        (root / os.fsdecode(b"bad\xff.yaml")).write_text("tenants: {}\n", encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        pytest.skip(f"this filesystem takes no such name ({exc})")
    with pytest.raises(InputChanged, match="changed after it was judged"):
        pc.refuse_changed(root, "da-guard served-values: exit 2")


def test_the_guard_reports_exit_2_on_a_changed_tree_is_the_flags_refusal(tmp_path, fresh_declarations):
    """#3965 review: the default check's exit 2 (a tenant declared twice since
    the judgement) on a judged tree is the change, named by the flag — not a
    guard error row."""
    from _lib_exitcodes import InputChanged
    import _lib_tenant_values
    pc = fresh_declarations
    root = _judged_tree(tmp_path, pc)
    (root / "dup.yaml").write_text((root / "a.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(InputChanged, match="changed after it was judged"):
        _lib_tenant_values.load_guard_report(root)


def test_a_write_below_a_judged_tree_is_refused(tmp_path, fresh_declarations):
    """#3965 review: the precondition of a write is the judged tree's identity;
    a write whose --config-dir is a directory below that tree cannot carry it,
    so it goes through the tree's root."""
    import _lib_confedit
    pc = fresh_declarations
    root = _judged_tree(tmp_path, pc)
    (root / "team").mkdir()
    with pytest.raises(ValueError, match="written through the tree's root"):
        _lib_confedit.edit(root / "team", "t.yaml", [])


def test_one_directory_on_both_sides_of_config_diff_reads_alike(tmp_path, fresh_declarations, monkeypatch):
    """#3965 review: config-diff walks --old-dir and --new-dir apart; when they
    name one directory, the second walk must read the first's tree."""
    from _lib_exitcodes import InputChanged
    sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
    import config_diff as cd
    pc = fresh_declarations
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.yaml").write_text("tenants:\n  ta:\n    mysql_connections: \"80\"\n", encoding="utf-8")
    real, calls = cd._lib_parse_tree.run, []

    def run(conf_d, *args, **kwargs):
        doc = real(conf_d, *args, **kwargs)
        calls.append(conf_d)
        if len(calls) == 2:  # the other side's walk read another tree
            doc = {**doc, "config_hash": "f" * 64}
        return doc
    monkeypatch.setattr(cd._lib_parse_tree, "run", run)
    monkeypatch.setattr(sys, "argv", ["config_diff.py", "--old-dir", str(d), "--new-dir", str(d)])
    with pytest.raises(InputChanged, match="changed after it was judged"):
        cd.main()
    assert len(calls) >= 2

# ── #3901: a selected file gone when the Go readers run ──────────────────
# The gate's file mode fires where PYTHON looks at a file under the input;
# onboard and silencer-drift-check hand the selected files to da-crdecode /
# da-read by path and took no look of their own, so a file gone then was the
# reader's "no such file": one error beside a report made from the rest
# (rc 2 with a partial document; onboard had printed its phase by then).

_VANISH = ("#!/bin/sh\n[ -e \"$DA_TEST_VICTIM\" ] && rm -f \"$DA_TEST_VICTIM\"\n"
           "exec \"$DA_TEST_REAL_{name}\" \"$@\"\n")


@pytest.mark.usefixtures("da_read_env")
@pytest.mark.parametrize("script, argv, flag", [
    # Not --dry-run: onboard had written its plan by the time it said rc 2.
    ("ops/onboard_platform.py", [a for a in _ONBOARD_RULES if a != "--dry-run"],
     "--rule-files"),
    ("ops/silencer_drift_check.py", _SILENCER_RULES, "--rule-source"),
])
def test_a_selected_file_gone_under_the_go_readers_is_refused_not_reported(
        tmp_path, script, argv, flag):
    rules, box = tmp_path / "rules", tmp_path / "box"
    _RULE_DIR["live"](rules)
    box.mkdir()
    (box / "silences.json").write_text("[]\n", encoding="utf-8")
    victim = rules / "sub" / "b.yaml"
    env = dict(os.environ, DA_LANG="en", DA_TEST_VICTIM=str(victim))
    for name, var in (("READ", "DA_READ_BINARY"), ("CRDECODE", "DA_CRDECODE_BINARY")):
        wrapper = tmp_path / f"vanish-{name.lower()}"
        wrapper.write_text(_VANISH.replace("{name}", name), encoding="utf-8")
        wrapper.chmod(0o755)
        env[f"DA_TEST_REAL_{name}"] = os.environ[var]
        env[var] = str(wrapper)
    args = [a.replace("{D}", str(rules)).replace("{BOX}", str(box)) for a in argv]
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "tools" / script), *args],
                          cwd=tmp_path, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300, check=False,
                          stdin=subprocess.DEVNULL)
    assert not victim.exists(), "the wrapper never ran: nothing was measured"
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout == "", f"a report made without {victim.name}:\n{proc.stdout}"
    assert f"ERROR: {flag} {victim}: " in proc.stderr, proc.stderr
    assert not crashed(Run(proc.returncode, proc.stdout, proc.stderr, {}, []))
    written = sorted(str(p.relative_to(box)) for p in box.rglob("*") if p.is_file())
    assert written == ["silences.json"], f"written before the refusal: {written}"
