"""Every `job` matcher in a rule pack names a job something scrapes (#4012).

`_lib_metric_sources.TENANT_EXPORTER_JOB` is the one name for the scrape job
every tenant rule pack selects; the scrape side is pinned to it by
`check_scrape_reachability.tenant_scrape_contract`. The rule side was not: the
packs spell `job="tenant-exporters"` as a literal dozens of times, and
`test_exporter_presence.py` covers only the presence-generated rules. A pack
whose literal drifts (a typo, a renamed job) reads a job nothing scrapes, and
every rule behind it returns empty without an error.

Two scopes, both read the way Prometheus loads them (`da-read prometheus-rules`):

- tenant packs (`_lib_consumer_read.tenant_rule_pack_files`): every `job`
  matcher is exactly `job="<TENANT_EXPORTER_JOB>"`;
- the declared platform packs (`PLATFORM_PACKS`): they also watch the
  platform's own jobs, so each `job` matcher is `=` one of the jobs the repo
  produces — the Operator install's (`operator_jobs()`, which includes the
  tenant job) or a raw-manifest scrape job with a fixed `job` value
  (`check_scrape_reachability.load_repo_jobs`).

PromQL quotes a label value with `"`, `'` or a backtick, and since Prometheus 3
may quote a label NAME too (`{"job"="x"}`); every spelling is read.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import _lib_consumer_read as cr
import _lib_metric_sources as ms

pytestmark = pytest.mark.usefixtures("da_read_env")

REPO = Path(__file__).resolve().parents[2]

# `job <op> <quoted value>`; the value in any of PromQL's three quotings, the
# name bare or quoted (Prometheus 3 UTF-8 label names). \b keeps `xjob=` and
# `job_name=` out; a `by (job)` has no operator.
_JOB_RE = re.compile(
    r"(?:\bjob|\"job\"|'job'|`job`)\s*(=~|!~|!=|=)\s*"
    r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|`[^`]*`)")


def job_matchers(expr: str) -> list[tuple[str, str]]:
    """(operator, unquoted value) for each `job` matcher in *expr*."""
    return [(op, quoted[1:-1]) for op, quoted in _JOB_RE.findall(expr)]


def job_violations(expr: str, allowed: "frozenset[str]") -> list[str]:
    """Each `job` matcher in *expr* that is not `job="<one of allowed>"`.
    Any operator but `=` is one too: a regex or negative matcher is a
    different selection than the scrape contract."""
    return [f'job{op}"{value}"' for op, value in job_matchers(expr)
            if not (op == "=" and value in allowed)]


def _exprs(files) -> list[tuple[str, str, str]]:
    """(pack file, rule name, expr) for every rule of *files*."""
    out = []
    for rf in cr.prometheus_rule_files(files):
        for group in rf.groups:
            for rule in group.get("rules") or []:
                name = rule.get("alert") or rule.get("record") or "?"
                out.append((Path(rf.path).name, name, str(rule.get("expr", ""))))
    return out


def _platform_jobs() -> frozenset:
    import check_scrape_reachability as csr  # noqa: PLC0415 — needs da-read/da-crdecode
    raw = {j["job_value"] for j in csr.load_repo_jobs() if j.get("job_value")}
    return frozenset(ms.operator_jobs() | raw)


def _check(exprs, allowed, what):
    bad = [f"{f}: {n}: {v}" for f, n, expr in exprs for v in job_violations(expr, allowed)]
    assert not bad, (
        f"{what} must select a job in {sorted(allowed)} with `=`:\n  " + "\n  ".join(bad))


def test_every_tenant_pack_job_matcher_is_the_tenant_exporter_job():
    exprs = _exprs(cr.tenant_rule_pack_files(REPO / "rule-packs"))
    # Not vacuous: the tenant packs held 64 job matchers when this was written
    # (#4012). A reader that returned nothing, or a scope that lost the packs,
    # would otherwise pass.
    assert sum(len(job_matchers(e)) for _f, _n, e in exprs) >= 50
    _check(exprs, frozenset({ms.TENANT_EXPORTER_JOB}), "tenant rule packs")


def test_every_platform_pack_job_matcher_is_a_scraped_job():
    platform = [p for p in cr.rule_pack_files(REPO / "rule-packs")
                if p not in set(cr.tenant_rule_pack_files(REPO / "rule-packs"))]
    assert platform, "no platform pack found: PLATFORM_PACKS lost its file"
    exprs = _exprs(platform)
    # The platform pack reads both the tenant job and its own (51 matchers
    # when this was written).
    assert sum(len(job_matchers(e)) for _f, _n, e in exprs) >= 30
    _check(exprs, _platform_jobs(), "platform rule packs")


def test_platform_job_set_is_the_produced_one():
    """The allowed set is computed, not listed; pin that it has the jobs the
    platform pack actually watches, so a reader that lost a source fails here
    rather than as a wall of false violations."""
    jobs = _platform_jobs()
    for job in (ms.TENANT_EXPORTER_JOB, "prometheus", "tenant-api", "threshold-exporter"):
        assert job in jobs, sorted(jobs)


T = frozenset({ms.TENANT_EXPORTER_JOB})


@pytest.mark.parametrize("expr, bad", [
    ('up{job="tenant-exporters"} == 0', []),
    ("up{job='tenant-exporters'} == 0", []),
    ("up{job=`tenant-exporters`} == 0", []),
    ('up{job="tenant-exporter"} == 0', ['job="tenant-exporter"']),
    ("up{job='tenant-exporter'} == 0", ['job="tenant-exporter"']),
    ("up{job=`tenant-exporter`} == 0", ['job="tenant-exporter"']),
    ('up{job=~"tenant-exporters"} == 0', ['job=~"tenant-exporters"']),
    ('up{job!="tenant-exporters"}', ['job!="tenant-exporters"']),
    ('max(x{job="tenant-exporters", instance="a"}) and y{job="node"}', ['job="node"']),
    ('sum(rate(x[5m])) by (job)', []),
    ('x{job_name="node", xjob="node"}', []),
    ('up{"job"="tenant-exporter"} == 0', ['job="tenant-exporter"']),
    ("{'up', 'job'='tenant-exporters'}", []),
    ('{"up", `job`=~"node"}', ['job=~"node"']),
])
def test_job_violations_reads_each_matcher(expr, bad):
    assert job_violations(expr, T) == bad
