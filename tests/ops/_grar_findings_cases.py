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
    "  mysql_connections: 80\n"
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
    return (f"tenants:\n  {t}:\n    mysql_connections: 70\n    _routing:\n"
            + receiver.format(t=t) + extra)


FIXTURES: dict[str, dict[str, str]] = {
    # Two healthy tenants: no finding beyond the dedup INFO line.
    "clean": {
        "_defaults.yaml": _DEFAULTS,
        "alpha.yaml": _tenant("alpha"),
        "beta.yaml": _tenant("beta", _EMAIL)
        + "    _severity_dedup: disable\n",
    },
    # ADR-007 domain policy: violations, an escalation miss, a leak, and
    # malformed constraints only --strict reports.
    "policy": {
        "_defaults.yaml": _DEFAULTS,
        "_domain_policy.yaml": (
            "domain_policies:\n"
            "  finance:\n"
            "    tenants: [alpha, beta, {nested: 1}]\n"
            "    constraints:\n"
            "      forbidden_receiver_types: [webhook]\n"
            "      allowed_receiver_types: [email, pagerduty]\n"
            "      max_repeat_interval: 1h\n"
            "      min_group_wait: banana\n"
            "      enforce_group_by: [tenant, severity]\n"
            "      require_critical_escalation: true\n"
            "  broken:\n"
            "    tenants: alpha\n"
        ),
        "alpha.yaml": _tenant("alpha"),
        "beta.yaml": _tenant(
            "beta",
            "      receiver:\n"
            "        type: pagerduty\n"
            "        service_key: abc123\n",
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
        "alpha.yaml": 'tenants:\n  alpha:\n    mysql_connections: "70\n',
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
    # ADR-007 --strict: a domain policy file nothing can be read from.
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
            "  mysql_connections: 80\n",
            "  mysql_connections: 80\n  mysql_threads_running: 50\n"),
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
}

MODES: dict[str, list[str]] = {
    "render": ["--dry-run"],
    "validate": ["--validate"],
    "strict": ["--strict", "--dry-run"],
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
    """Run the generator with no ``amtool`` on PATH (deterministic NOTICE)."""
    env = {k: v for k, v in os.environ.items() if k != "PATH"}
    env["PATH"] = os.devnull  # nothing resolvable: amtool is "not found"
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
