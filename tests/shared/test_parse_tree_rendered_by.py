"""`da-guard parse-tree`'s `rendered_by` held to the renders themselves (#2962).

Each case writes one platform carrier in one location of the shipped sample
conf.d, changes it on the PR side, renders both sides (served values and
routes, `_render_diff.compare`) and asserts that the views that changed are
exactly what parse-tree says reads that key there: `[]` must change nothing,
a reader must change its view. The oracle is the renders — Go's table is the
thing under test, so a rule that agrees with another table but not with what
the exporter and the route generator do is red here.

`exact` cases change something every listed reader renders, so the views
must equal the readers; the others only need the changed views to be among
them and not empty (a threshold in `tenants:` reaches no route).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

import _lib_parse_tree  # noqa: E402
import _render_diff as rd  # noqa: E402
import config_diff as cd  # noqa: E402

SAMPLE = ROOT / "components" / "threshold-exporter" / "config" / "conf.d"
AT = "2026-10-10T00:00:00Z"
VIEW = {"exporter": rd.VIEW_SERVED, "generator": rd.VIEW_ROUTES}

pytestmark = pytest.mark.usefixtures("da_guard_env")


def _sub(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} occurs {text.count(old)} times in {path}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def _put(rel: str, text: str):
    return lambda d: (d / rel).write_text(text, encoding="utf-8")


def _append(rel: str, text: str):
    def f(d: Path) -> None:
        with open(d / rel, "a", encoding="utf-8") as fh:
            fh.write(text)
    return f


def _both(*fs):
    return lambda d: [f(d) for f in fs]


ENFORCED = ("_routing_enforced:\n  enabled: true\n  receiver:\n    type: webhook\n"
            "    url: \"https://noc.example.com/hook\"\n")
BIND_P1 = lambda d: _sub(d / "db-a.yaml", '    mysql_connections: "70"\n',  # noqa: E731
                         '    mysql_connections: "70"\n    _profile: p1\n')
NESTED_CARRIER = "examples/_defaults.yaml"

# (id, file, key, setup on both sides, edit on the PR side, exact)
CASES = [
    # the root defaults carrier
    ("root-carrier.defaults", "_defaults.yaml", "defaults", None,
     lambda d: _sub(d / "_defaults.yaml", "container_memory: 85", "container_memory: 86"), True),
    ("root-carrier.state_filters", "_defaults.yaml", "state_filters", None,
     lambda d: _sub(d / "_defaults.yaml", 'reasons: ["CrashLoopBackOff"]\n    severity: "critical"',
                    'reasons: ["CrashLoopBackOff"]\n    severity: "warning"'), True),
    ("root-carrier._routing_defaults", "_defaults.yaml", "_routing_defaults", None,
     lambda d: _sub(d / "_defaults.yaml", 'group_wait: "30s"', 'group_wait: "99s"'), True),
    ("root-carrier._routing_enforced", "_defaults.yaml", "_routing_enforced",
     _append("_defaults.yaml", "\n" + ENFORCED),
     lambda d: _sub(d / "_defaults.yaml", "https://noc.example.com/hook", "https://noc.example.com/h2"), True),
    ("root-carrier.profiles", "_defaults.yaml", "profiles",
     _both(_append("_defaults.yaml", "\nprofiles:\n  p1:\n    container_memory: 50\n"), BIND_P1),
     lambda d: _sub(d / "_defaults.yaml", "container_memory: 50", "container_memory: 51"), True),
    ("root-carrier.tenants", "_defaults.yaml", "tenants",
     _append("_defaults.yaml", "\ntenants:\n  db-a:\n    container_memory: \"60\"\n"),
     lambda d: _sub(d / "_defaults.yaml", 'container_memory: "60"', 'container_memory: "61"'), False),
    ("root-carrier.optional_overrides", "_defaults.yaml", "optional_overrides",
     lambda d: _sub(d / "db-a.yaml", '    mysql_connections: "70"\n',
                    '    mysql_connections: "70"\n    oracle_process_count: "100"\n'),
     lambda d: _sub(d / "_defaults.yaml", "  - oracle_process_count", "  - oracle_process_countx"), True),
    # another root `_` file
    ("root-other.optional_overrides", "_extra.yaml", "optional_overrides",
     _both(lambda d: _sub(d / "db-a.yaml", '    mysql_connections: "70"\n',
                          '    mysql_connections: "70"\n    x_opt_2962: "100"\n'),
           _put("_extra.yaml", "optional_overrides:\n  - x_opt_2962\n")),
     lambda d: _sub(d / "_extra.yaml", "  - x_opt_2962\n", "  - x_opt_2962x\n"), True),
    ("root-routing-profiles.routing_profiles", "_routing_profiles.yaml", "routing_profiles",
     _both(lambda d: _sub(d / "db-a.yaml", '    mysql_connections: "70"\n',
                          '    mysql_connections: "70"\n    _routing_profile: root-p\n'),
           _put("_routing_profiles.yaml", "routing_profiles:\n  root-p:\n    receiver:\n      type: webhook\n"
                                          "      url: \"https://hooks.example.com/rp\"\n")),
     lambda d: _sub(d / "_routing_profiles.yaml", "https://hooks.example.com/rp", "https://hooks.example.com/r2"),
     True),
    ("root-routing-profiles.tenants", "_routing_profiles.yaml", "tenants",
     _put("_routing_profiles.yaml", "routing_profiles: {}\ntenants:\n  db-a:\n    container_memory: \"60\"\n"),
     lambda d: _sub(d / "_routing_profiles.yaml", 'container_memory: "60"', 'container_memory: "61"'), False),
    ("root-other.defaults", "_extra.yaml", "defaults",
     _put("_extra.yaml", "defaults:\n  container_memory: 70\n"),
     lambda d: _sub(d / "_extra.yaml", "container_memory: 70", "container_memory: 71"), True),
    ("root-other.state_filters", "_extra.yaml", "state_filters",
     _put("_extra.yaml", "state_filters:\n  x_oom:\n    reasons: [OOMKilled]\n    severity: critical\n"),
     lambda d: _sub(d / "_extra.yaml", "severity: critical", "severity: warning"), True),
    ("root-other._routing_defaults", "_routing_extra.yaml", "_routing_defaults", None,
     _put("_routing_extra.yaml", "_routing_defaults:\n  receiver:\n    type: webhook\n"
                                 "    url: \"https://hooks.example.com/x\"\n"), False),
    ("root-other._routing_enforced", "_noc.yaml", "_routing_enforced", None,
     _put("_noc.yaml", ENFORCED), True),
    ("root-other.routing_profiles", "_extra.yaml", "routing_profiles",
     _put("_extra.yaml", "routing_profiles:\n  domain-finance-tier1:\n    receiver:\n      type: webhook\n"
                         "      url: \"https://hooks.example.com/f\"\n"),
     lambda d: _sub(d / "_extra.yaml", "https://hooks.example.com/f", "https://hooks.example.com/g"), True),
    ("root-profiles-file.profiles", "_profiles.yaml", "profiles",
     _both(_put("_profiles.yaml", "profiles:\n  p1:\n    container_memory: 50\n"), BIND_P1),
     lambda d: _sub(d / "_profiles.yaml", "container_memory: 50", "container_memory: 51"), True),
    ("root-platform.tenants._routing", "_platform.yaml", "tenants",
     _put("_platform.yaml", "tenants:\n  db-a:\n    container_memory: \"60\"\n"),
     lambda d: _put("_platform.yaml", "tenants:\n  db-a:\n    container_memory: \"60\"\n    _routing:\n"
                                      "      receiver:\n        type: webhook\n"
                                      "        url: \"https://hooks.example.com/a\"\n")(d), True),
    # a directory's defaults carrier
    ("nested-carrier.defaults", NESTED_CARRIER, "defaults",
     _put(NESTED_CARRIER, "defaults:\n  container_memory: 80\n"),
     lambda d: _sub(d / NESTED_CARRIER, "container_memory: 80", "container_memory: 81"), True),
    ("nested-carrier.state_filters", NESTED_CARRIER, "state_filters",
     _put(NESTED_CARRIER, "defaults:\n  container_memory: 80\nstate_filters:\n  x_oom:\n"
                          "    reasons: [OOMKilled]\n    severity: critical\n"),
     lambda d: _sub(d / NESTED_CARRIER, "severity: critical", "severity: warning"), True),
    ("nested-carrier._routing_defaults", NESTED_CARRIER, "_routing_defaults",
     _put(NESTED_CARRIER, "defaults:\n  container_memory: 80\n_routing_defaults:\n  group_wait: \"45s\"\n"),
     lambda d: _sub(d / NESTED_CARRIER, 'group_wait: "45s"', 'group_wait: "46s"'), True),
    ("nested-carrier.optional_overrides", NESTED_CARRIER, "optional_overrides",
     _put(NESTED_CARRIER, "defaults:\n  container_memory: 80\noptional_overrides:\n  - x_opt_2962\n"),
     lambda d: _sub(d / NESTED_CARRIER, "  - x_opt_2962\n", "  - x_opt_2962x\n"), True),
    ("nested-carrier.profiles", NESTED_CARRIER, "profiles",
     _put(NESTED_CARRIER, "defaults:\n  container_memory: 80\nprofiles:\n  p1:\n    container_memory: 50\n"),
     lambda d: _sub(d / NESTED_CARRIER, "container_memory: 50", "container_memory: 51"), True),
    ("nested-carrier.tenants", NESTED_CARRIER, "tenants",
     _put(NESTED_CARRIER, "defaults:\n  container_memory: 80\ntenants:\n  es-prod:\n"
                          "    container_memory: \"60\"\n"),
     lambda d: _sub(d / NESTED_CARRIER, 'container_memory: "60"', 'container_memory: "61"'), True),
    # another `_` file below the root
    ("nested-other.defaults", "examples/_defaults-multidb.yaml", "defaults", None,
     lambda d: _sub(d / "examples/_defaults-multidb.yaml", "  mysql_connections: 80\n",
                    "  mysql_connections: 81\n"), True),
    ("nested-other.tenants", "examples/_platform.yaml", "tenants",
     _put("examples/_platform.yaml", "tenants:\n  es-prod:\n    container_memory: \"60\"\n"),
     lambda d: _sub(d / "examples/_platform.yaml", 'container_memory: "60"', 'container_memory: "61"'), True),
    ("nested-profiles-file-yml.routing_profiles", "examples/_routing_profiles.yml", "routing_profiles",
     lambda d: (d / "examples/_routing_profiles.yaml").rename(d / "examples/_routing_profiles.yml"),
     lambda d: _sub(d / "examples/_routing_profiles.yml", 'service_key: "finance-tier1-key"',
                    'service_key: "finance-tier1-k2"'), True),
    ("nested-profiles-file.routing_profiles", "examples/_routing_profiles.yaml", "routing_profiles", None,
     lambda d: _sub(d / "examples/_routing_profiles.yaml", 'service_key: "finance-tier1-key"',
                    'service_key: "finance-tier1-k2"'), True),
]


@pytest.mark.parametrize("case_id,rel,key,setup,edit,exact", CASES, ids=[c[0] for c in CASES])
def test_rendered_by_matches_the_renders(tmp_path, case_id, rel, key, setup, edit, exact):
    old, new = tmp_path / "old", tmp_path / "new"
    shutil.copytree(SAMPLE, old)
    shutil.copytree(SAMPLE, new)
    if setup:
        setup(old)
        setup(new)
    edit(new)
    doc = _lib_parse_tree.run(new, None, 60)
    readers = doc["rendered_by"][rel][key]
    diff = rd.compare(old, new, AT, served_report=cd.served_sections)
    report = cd.build_report(diff, old, new)
    statuses = {name: v.status for name, v in report["views"].items()}
    assert set(statuses.values()) == {"computed"}, statuses
    changed = {rd.VIEW_SERVED} if cd._served_changed(report) else set()
    if report["routes"] or report["receivers"] or report["inhibits"]:
        changed.add(rd.VIEW_ROUTES)
    claimed = {VIEW[r] for r in readers}
    if exact:
        assert changed == claimed, (case_id, readers, sorted(changed))
    else:
        assert changed and changed <= claimed, (case_id, readers, sorted(changed))


def test_every_rendered_carrier_is_answered():
    """config-diff's carriers are a subset of what rendered_by answers for."""
    import re
    src = (ROOT / "components/threshold-exporter/app/cmd/da-guard/rendered_by.go").read_text(encoding="utf-8")
    block = re.search(r"var renderedByKeys = \[\]string\{(.*?)\}", src, re.S).group(1)
    answered = set(re.findall(r'"([^"]+)"', block))
    assert set(cd.RENDERED_CARRIERS) <= answered, set(cd.RENDERED_CARRIERS) - answered
