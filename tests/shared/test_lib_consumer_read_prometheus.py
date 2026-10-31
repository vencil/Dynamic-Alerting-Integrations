"""`_lib_consumer_read`'s Prometheus readings (#3206, ADR-036 §12):
`prometheus_config` (`da-read prometheus`) and the rule-file batch
(`da-read prometheus-rules`).

The rule-file interface is built so a refused file cannot pass as an empty
one: `prometheus_rule_files` raises when any file is refused, and the
per-file results a lint reports from raise when a refused file's contents are
read. The last test reads every rule file the repo ships through it — the
rule packs and the rules held in the monitoring ConfigMaps (their outer
layer read the Kubernetes way) — so the next step's readers start from files
Prometheus loads.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import _lib_consumer_read as cr

REPO = Path(__file__).resolve().parents[2]

GOOD = "groups:\n- name: g\n  rules:\n  - {alert: A, expr: up == 0, for: 300s, labels: {tenant: 010}}\n"
BAD_PROMQL = "groups:\n- name: g\n  rules:\n  - {alert: B, expr: 'rate(x[5m]'}\n"


def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


@pytest.mark.parametrize("source", [
    "global: {scrape_interval: 90s}\n",
    b"global: {scrape_interval: 90s}\n",
])
def test_prometheus_config_from_text(source) -> None:
    assert cr.prometheus_config(source) == {"global": {"scrape_interval": "1m30s"}}


def test_prometheus_config_from_a_path(tmp_path) -> None:
    p = Path(_write(tmp_path, "prometheus.yml", "rule_files: [a.yml]\n"))
    assert cr.prometheus_config(p) == {"rule_files": ["a.yml"]}


def test_prometheus_config_refusal_carries_the_reason() -> None:
    with pytest.raises(cr.ConsumerReadError) as exc_info:
        cr.prometheus_config("scrape_configz: []\n")
    assert any("field scrape_configz not found" in ln for ln in exc_info.value.stderr_lines)


def test_prometheus_config_without_da_read(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DA_READ_BINARY", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(cr.ConsumerReadError, match="da-read was not found"):
        cr.prometheus_config("")


def test_rule_files_in_order_as_prometheus_reads_them(tmp_path) -> None:
    a = _write(tmp_path, "a.yml", GOOD)
    b = _write(tmp_path, "b.yml", "groups: []\n")
    got = cr.prometheus_rule_files([a, Path(b)])
    assert [r.path for r in got] == [a, b]
    assert got[0].groups == [{"name": "g", "rules": [
        {"alert": "A", "expr": "up == 0", "for": "5m", "labels": {"tenant": "010"}}]}]
    assert got[1].groups == [] and got[1].problem is None
    assert cr.prometheus_rule_files([]) == []


def test_a_refused_rule_file_cannot_be_read_as_empty(tmp_path) -> None:
    a = _write(tmp_path, "a.yml", GOOD)
    b = _write(tmp_path, "b.yml", BAD_PROMQL)
    missing = str(tmp_path / "missing.yml")
    # The default interface raises, carrying every file's result.
    with pytest.raises(cr.RuleFilesRefused) as exc_info:
        cr.prometheus_rule_files([a, b, missing])
    refused = exc_info.value
    assert [r.path for r in refused.refused] == [b, missing]
    assert [r.path for r in refused.results] == [a, b, missing]
    assert any(ln.startswith(f"{b}: ") and "parse error" in ln for ln in refused.stderr_lines)
    # The per-file interface returns them; a refused one's contents raise.
    results = cr.prometheus_rule_file_results([a, b, missing])
    assert results[0].problem is None and results[0].groups
    assert "parse error" in results[1].problem
    for r in results[1:]:
        with pytest.raises(cr.RuleFileRefused) as one:
            r.groups
        assert one.value.path == r.path
        with pytest.raises(cr.RuleFileRefused):
            r.documents


def test_a_second_document_with_content_is_refused(tmp_path) -> None:
    a = _write(tmp_path, "a.yml", GOOD + "---\ngroups: []\n")
    [r] = cr.prometheus_rule_file_results([a])
    assert "YAML document 2 of the file has content" in r.problem


@pytest.mark.parametrize("stderr", [
    "panic: runtime error",          # a Go crash exits 2 too
    "fatal error: out of memory",
    "",                              # rc 2 without a reason
])
def test_a_rule_file_run_that_is_not_a_refusal_is_not_input_refused(tmp_path, stderr) -> None:
    """The rule-file batch takes the same judgement of "refused" as every
    other reading (`_refused`): rc 2 with JSON that refuses a file is still
    no answer about the file when stderr is a crash or empty."""
    a = _write(tmp_path, "a.yml", GOOD)
    payload = ('{"files": [{"path": "%s", "documents": null, '
               '"problem": {"message": "x"}}]}' % a)
    fake = tmp_path / "fake-da-read"
    say = f"echo '{stderr}' >&2; " if stderr else ""
    fake.write_text(f"#!/bin/sh\necho '{payload}'; {say}exit 2\n", encoding="utf-8")
    fake.chmod(0o755)
    with pytest.raises(cr.ConsumerReadError) as exc:
        cr.prometheus_rule_file_results([a], binary=str(fake))
    assert not isinstance(exc.value, cr.InputRefused)


def _repo_rule_files(tmp_path: Path) -> "list[str]":
    # #3471: the repo's rule files as the declaration lists them.
    files = [str(p) for p in cr.RULE_PACKS.files_under(REPO)]
    assert len(files) >= 10, files
    configmaps = [str(p) for p in cr.CONFIGMAP_RULES.files_under(REPO)]
    assert configmaps
    for cm in configmaps:
        for i, doc in enumerate(cr.kubernetes_documents(cm)):
            for key, text in ((doc or {}).get("data") or {}).items():
                files.append(_write(tmp_path, f"{Path(cm).stem}.{i}.{key}", text))
    return files


def test_every_rule_file_the_repo_ships_is_read(tmp_path) -> None:
    """The rule packs and the ConfigMap-held rule files, in one batch: each is
    a rule file Prometheus loads, with at least one group."""
    files = _repo_rule_files(tmp_path)
    results = cr.prometheus_rule_files(files)
    assert len(results) == len(files)
    empty = [os.path.basename(r.path) for r in results if not r.groups]
    assert not empty, empty
