"""conf.d fixtures + runner shared by ``test_grar_findings.py`` (#2766).

Kept apart from the test module so the golden capture
(``python3 tests/ops/_grar_findings_cases.py --write-golden``) and the tests
build byte-identical trees and run the generator the same way.

The golden file (``grar_findings_golden.json``) holds stdout / stderr / rc of
every case as the generator printed them BEFORE structured findings existed
(captured on ``origin/main`` before #2766 step 1): structured findings are
additive, so that output must never move. Regenerate it only for a change
that is meant to alter the generator's text, and say so in the commit.

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
GOLDEN = Path(__file__).resolve().parent / "grar_findings_golden.json"
CONFDIR_TOKEN = "<CONFDIR>"

_DEFAULTS = (
    "defaults:\n"
    "  mysql_connection_usage_percent: 80\n"
    "_routing_defaults:\n"
    "  group_by: [alertname, tenant]\n"
    "  group_wait: 30s\n"
    "  repeat_interval: 4h\n"
)
_WEBHOOK = ("      receiver:\n"
            "        type: webhook\n"
            "        url: https://hooks.example.com/{t}\n")
_EMAIL = ("      receiver:\n"
          "        type: email\n"
          "        to: [oncall@example.com]\n"
          "        from: alerting@example.com\n"
          "        smarthost: smtp.example.com:587\n")


def _tenant(t: str, receiver: str = _WEBHOOK, extra: str = "") -> str:
    return (f"tenants:\n  {t}:\n    mysql_connection_usage_percent: 70\n    _routing:\n"
            + receiver.format(t=t) + extra)


FIXTURES: dict[str, dict[str, str]] = {
    # Two healthy tenants: no finding beyond the dedup INFO line.
    "clean": {
        "_defaults.yaml": _DEFAULTS,
        "alpha.yaml": _tenant("alpha"),
        "beta.yaml": _tenant("beta", _EMAIL)
        + "    _severity_dedup: disable\n",
    },
    # ADR-007 domain policy: violations, an escalation miss and a leak.
    # (#3116: the malformed constraints it also held refuse the whole tree
    # in every mode now — they moved to `policy_unusable`.)
    "policy": {
        "_defaults.yaml": _DEFAULTS,
        "_domain_policy.yaml": (
            "domain_policies:\n"
            "  finance:\n"
            "    tenants: [alpha, beta]\n"
            "    constraints:\n"
            "      forbidden_receiver_types: [webhook]\n"
            "      allowed_receiver_types: [email, pagerduty]\n"
            "      max_repeat_interval: 1h\n"
            "      enforce_group_by: [tenant, severity]\n"
            "      require_critical_escalation: true\n"
        ),
        "alpha.yaml": _tenant("alpha"),
        "beta.yaml": _tenant(
            "beta",
            "      receiver:\n"
            "        type: pagerduty\n"
            # #1560 ⑤-b: a reference, no value written out (doubled
            # braces: _tenant formats the receiver text).
            "        service_key: {{secret: {{key: abc123}}}}\n",
            "      group_by: [tenant, severity]\n"
            "      repeat_interval: 30m\n"
            "      routes:\n"
            "        - match: {team: app}\n"
            "          receiver:\n"
            "            type: webhook\n"
            "            url: https://hooks.example.com/app\n"),
    },
    # Entries the generator drops or values it replaces.
    "skipped": {
        "_defaults.yaml": _DEFAULTS,
        "alpha.yaml": _tenant(
            "alpha", _WEBHOOK,
            "      group_wait: 1.5h\n"
            "      group_by: [alertname, 8]\n"
            "      overrides:\n"
            "        - alertname: A\n"
            "          metric_group: B\n"
            "          receiver: {type: webhook, url: 'https://hooks.example.com/o'}\n"
            "        - alertname: C\n"
            "      routes:\n"
            "        - match: {team: app}\n"
            "    unknown_threshold_key: 5\n"),
        "beta.yaml": _tenant("beta", "      receiver:\n        type: carrier-pigeon\n"),
        "gamma.yaml": "tenants:\n  gamma:\n    _routing: [nope]\n",
    },
    # #1460: one tenant file does not parse.
    "unreadable": {
        "_defaults.yaml": _DEFAULTS,
        "alpha.yaml": 'tenants:\n  alpha:\n    mysql_connection_usage_percent: "70\n',
        "beta.yaml": _tenant("beta"),
    },
    # #2315: one tenant id in two files.
    "duplicate": {
        "_defaults.yaml": _DEFAULTS,
        "alpha.yaml": _tenant("alpha"),
        "alpha-again.yaml": _tenant("alpha"),
    },
    # #2326: `_routing_enforced` below the root.
    "tree": {
        "_defaults.yaml": _DEFAULTS,
        "team/_defaults.yaml": "_routing_enforced:\n  enabled: false\n",
        "team/alpha.yaml": _tenant("alpha"),
    },
    # ADR-035 D3: a tenant id the rule refuses.
    "invalid_id": {
        "_defaults.yaml": _DEFAULTS,
        "alpha.yaml": _tenant("alpha"),
        "Bad_Id.yaml": _tenant("Bad_Id"),
    },
    # #3116: policies that cannot be enforced as written — refused in every
    # mode (a `tenants` entry that names no tenant, an unreadable bound, a
    # `tenants` that is not a list).
    "policy_unusable": {
        "_defaults.yaml": _DEFAULTS,
        "_domain_policy.yaml": (
            "domain_policies:\n"
            "  finance:\n"
            "    tenants: [alpha, beta, {nested: 1}]\n"
            "    constraints:\n"
            "      forbidden_receiver_types: [webhook]\n"
            "      min_group_wait: banana\n"
            "  broken:\n"
            "    tenants: alpha\n"
        ),
        "alpha.yaml": _tenant("alpha"),
    },
    # ADR-007: a domain policy file nothing can be read from (#3116: refused
    # in every mode).
    "policy_file": {
        "_defaults.yaml": _DEFAULTS,
        "_domain_policy.yaml": "domain_policies:\n  - one\n  - two\n",
        "alpha.yaml": _tenant("alpha"),
    },
    # `_routing_enforced` ({{tenant}} shape) with a bad group_by and timing.
    "enforced": {
        "_defaults.yaml": _DEFAULTS + (
            "_routing_enforced:\n"
            "  enabled: true\n"
            "  receiver:\n"
            "    type: webhook\n"
            "    url: https://noc.example.com/{{tenant}}\n"
            "  group_by: [alertname, alertname]\n"
            "  group_wait: 30m1h\n"),
        "alpha.yaml": _tenant("alpha"),
    },
    # Advisory lines: a mistyped key, an unknown profile, a clamped value.
    "advisory": {
        "_defaults.yaml": _DEFAULTS.replace(
            "  mysql_connection_usage_percent: 80\n",
            "  mysql_connection_usage_percent: 80\n  mysql_threads_running: 50\n"),
        "alpha.yaml": _tenant("alpha", _WEBHOOK,
                              "      group_wait: 1s\n")
        + "    mysql_conections: 40\n    _routing_profile: nowhere\n",
    },
    # #2326 (d): a subtree policy naming a tenant outside its subtree.
    "scope": {
        "_defaults.yaml": _DEFAULTS,
        "team/_domain_policy.yaml": (
            "domain_policies:\n"
            "  local:\n"
            "    tenants: [alpha, beta]\n"
            "    constraints:\n"
            "      allowed_receiver_types: [email]\n"),
        "alpha.yaml": _tenant("alpha"),
        "team/beta.yaml": _tenant("beta"),
    },
    # #2279: two generated receivers with one name.
    "collision": {
        "_defaults.yaml": _DEFAULTS,
        "a.yaml": _tenant(
            "a", _WEBHOOK,
            "      routes:\n"
            "        - match: {team: x}\n"
            "          receiver: {type: webhook, url: 'https://hooks.example.com/r'}\n"),
        "a-route-0.yaml": _tenant("a-route-0"),
    },
    # #3703: findings that name a file below the root — a profile defined in
    # another directory, a level's defaults the generator does not use, a
    # subtree policy naming a tenant of a sibling subtree, a subtree with an
    # empty policy block, an empty `tenants:`.
    "subtree": {
        "_defaults.yaml": _DEFAULTS,
        "_enforced.yaml": (
            "_routing_enforced:\n"
            "  enabled: true\n"
            "  receiver: {type: webhook, url: 'https://noc.example.com/x'}\n"
            "  match: {severity: critical}\n"),
        "alpha.yaml": _tenant("alpha") + "    _routing_profile: p\n",
        "team/_routing_profiles.yaml": (
            "routing_profiles:\n"
            "  p:\n"
            "    receiver: {type: webhook, url: 'https://hooks.example.com/p'}\n"
            "    bogus_key: 1\n"),
        "team/_defaults.yaml": (
            "_routing_defaults: [1]\n"
            "defaults:\n"
            "  _routing:\n"
            "    receiver: {type: webhook, url: 'https://hooks.example.com/d'}\n"),
        "team/_defaults.yml": "defaults: {}\n",
        "team/_extra.yaml": "tenants:\n  beta:\n    mysql_connection_usage_percent: 1\n",
        "team/beta.yaml": _tenant("beta"),
        "team/_domain_policy.yaml": (
            "domain_policies:\n"
            "  local:\n"
            "    tenants: [gamma]\n"
            "    constraints:\n"
            "      allowed_receiver_types: [webhook]\n"),
        "other/gamma.yaml": _tenant("gamma"),
        "other/_domain_policy.yaml": "domain_policies: {}\n",
        "other/_defaults.yaml": (
            "_routing_defaults:\n"
            "  routes:\n"
            "    - match: {team: x}\n"
            "      receiver: {type: webhook, url: 'https://hooks.example.com/r'}\n"),
        "fourth/_routing_profiles.yaml": "routing_profiles: [1]\n",
        "delta.yaml": "tenants:\n",
    },
    # #3703: the same below the root, each refusing the whole tree in every
    # mode — a subtree policy that cannot be used, a policy block in a file
    # policies are not read from, a profile's receiver field of the wrong
    # type, a file reference under dropped routes.
    "subtree_refused": {
        "_defaults.yaml": _DEFAULTS,
        "_enforced.yaml": "_routing_enforced:\n  enabled: 'true'\n",
        "alpha.yaml": _tenant("alpha"),
        "team/_domain_policy.yaml": (
            "domain_policies:\n"
            "  finance:\n"
            "    tenants: [alpha, {nested: 1}]\n"
            "    constraints:\n"
            "      min_group_wait: banana\n"),
        "other/_domain_policy.yaml": "domain_policies: [1]\n",
        "third/_extra.yaml": "domain_policies: {}\n",
        "team/_routing_profiles.yaml": (
            "routing_profiles:\n"
            "  p:\n"
            "    receiver: {type: webhook, url: 'https://hooks.example.com/p',"
            " send_resolved: 'yes'}\n"
            "    group_by: alertname\n"),
        "other/_defaults.yaml": (
            "_routing_defaults:\n"
            "  routes:\n"
            "    - match: {team: x}\n"
            "      receiver:\n"
            "        type: webhook\n"
            "        url: 'https://hooks.example.com/r'\n"
            "        http_config: {bearer_token_file: /etc/x}\n"),
    },
    # #3703: findings that block the routing plane below the root — one
    # profile name in two directories, a subtree taking the receiver away.
    "subtree_blocking": {
        "_defaults.yaml": _DEFAULTS,
        "alpha.yaml": _tenant("alpha"),
        "team/_routing_profiles.yaml": (
            "routing_profiles:\n"
            "  p:\n"
            "    receiver: {type: webhook, url: 'https://hooks.example.com/p'}\n"),
        "other/_routing_profiles.yaml": (
            "routing_profiles:\n"
            "  p:\n"
            "    receiver: {type: webhook, url: 'https://hooks.example.com/q'}\n"),
        "team/_defaults.yaml": "_routing_defaults:\n  receiver: null\n",
        "team/beta.yaml": _tenant("beta"),
    },
    # #1560 ⑤-b: a credential written out in a tenant file.
    "credential": {
        "_defaults.yaml": _DEFAULTS,
        "alpha.yaml": _tenant("alpha"),
        "beta.yaml": _tenant(
            "beta",
            "      receiver:\n"
            "        type: slack\n"
            "        api_url: 'https://hooks.slack.com/services/T/B/X'\n"),  # trufflehog:ignore
    },
    # #3621 / #3269: `_severity_dedup` the exporter does not read, and one
    # written with a time window — refused in every mode.
    "dedup": {
        "_defaults.yaml": _DEFAULTS,
        "alpha.yaml": _tenant("alpha") + "    _severity_dedup: maybe\n",
        "beta.yaml": _tenant("beta") + (
            "    _severity_dedup:\n"
            "      default: enable\n"
            "      overrides:\n"
            "        - {window: '01:00-02:00', value: disable}\n"),
    },
    # A tenant body that is not a mapping: this reader skips the tenant, the
    # exporter's load drops the whole file (`ParseFailedError`).
    "decode": {
        "_defaults.yaml": _DEFAULTS,
        "alpha.yaml": _tenant("alpha"),
        "beta.yaml": "tenants:\n  beta: 3\n",
    },
    # `tenants:` that is a list: the file is unreadable as a tenant file.
    "tenants_list": {
        "_defaults.yaml": _DEFAULTS,
        "alpha.yaml": _tenant("alpha"),
        "beta.yaml": "tenants: [a]\n",
    },
    # ADR-038: the key verdict's kinds that name the file writing the key.
    "keys": {
        "_defaults.yaml": _DEFAULTS.replace(
            "  mysql_connection_usage_percent: 80\n",
            "  mysql_connection_usage_percent: 80\n  redis_memory: 70\n"),
        "_profiles.yaml": "profiles:\n  unbound:\n    zz_unknown_key: '1'\n",
        "alpha.yaml": _tenant("alpha") + (
            "    mysql_slave_lagg: disable\n"
            "    redis_memory: disable\n"
            "    redis_memory_critical: \"90\"\n"),
        "beta.yaml": (
            "tenants:\n  beta:\n"
            "    mysql_connection_usage_percent:\n"
            "      default: \"70\"\n"
            "      overrides:\n"
            "        - {window: '22:00-06:00', value: \"1000\"}\n"
            "        - {window: '01:00-03:00', value: \"5\"}\n"
            "    _silent_mode:\n"
            "      default: disable\n"
            "      overrides:\n"
            "        - {window: '01:00-02:00', value: all}\n"
            "    _metadata: {dbType: x}\n"),
    },
}

#: Trees whose run ends in a line that is not itemised as a finding yet, so
#: they are not in ``CASES`` (the findings contract above would fail on
#: them) — only the #3703 file-name guard
#: (``test_confd_filename_canary_matrix.py``) runs them, as it runs every case.
UNITEMISED: dict[str, dict[str, str]] = {
    # A tenant this reader loads beside one the exporter's load drops with
    # its whole file: `severity_dedup_failure_exit`'s rc-2 line.
    "decode_mixed": {
        "_defaults.yaml": _DEFAULTS,
        "alpha.yaml": _tenant("alpha"),
        "beta.yaml": "tenants:\n  beta: 3\n  gamma:\n    mysql_connection_usage_percent: 70\n",
    },
}

MODES: dict[str, list[str]] = {
    "output-alertmanager": ["--output-alertmanager", "--dry-run"],
    "validate": ["--validate"],
    "strict": ["--output-alertmanager", "--strict", "--dry-run"],
    "validate_strict": ["--validate", "--strict"],
}

CASES: list[tuple[str, str]] = [(f, m) for f in FIXTURES for m in MODES]


def build_tree(root: Path, fixture: str) -> Path:
    """Write *fixture* under ``root/conf.d`` and return that directory."""
    d = root / "conf.d"
    for rel, body in FIXTURES[fixture].items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return d


def run_gar(confdir: Path, argv: list[str],
            extra: list[str] | None = None) -> subprocess.CompletedProcess:
    """Run the generator with an empty PATH (nothing installed here changes
    its output; da-guard / da-read / da-crdecode come from their env vars)."""
    env = {k: v for k, v in os.environ.items() if k != "PATH"}
    env["PATH"] = os.devnull  # nothing resolvable on PATH
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-s", str(GAR), "--config-dir", str(confdir),
         *argv, *(extra or [])],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
        env=env)


def normalise(text: str, confdir: Path) -> str:
    return text.replace(str(confdir), CONFDIR_TOKEN)


def capture(root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for fixture, mode in CASES:
        base = root / f"{fixture}-{mode}"
        base.mkdir(parents=True)
        d = build_tree(base, fixture)
        res = run_gar(d, MODES[mode])
        out[f"{fixture}/{mode}"] = {
            "rc": res.returncode,
            "stdout": normalise(res.stdout, d),
            "stderr": normalise(res.stderr, d),
        }
    return out


if __name__ == "__main__":  # pragma: no cover - golden (re)capture
    import tempfile
    if sys.argv[1:] != ["--write-golden"]:
        sys.exit("usage: _grar_findings_cases.py --write-golden")
    with tempfile.TemporaryDirectory() as tmp:
        data = capture(Path(tmp))
    GOLDEN.write_text(json.dumps(data, indent=1, ensure_ascii=False,
                                 sort_keys=True) + "\n",
                      encoding="utf-8", newline="\n")
    print(f"wrote {GOLDEN} ({len(data)} cases)")
