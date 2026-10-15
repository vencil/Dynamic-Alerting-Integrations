"""#2901（ADR-036 §6）：部署端 policy 檔的 `min_generator_version`。

- 與 `allowed_domains` 同一份解析（`load_policy_document`），值必須是字串
  ``X.Y.Z``；其他寫法是 policy 不可用（`PolicyInputError`，CLI exit 2）。
- 產生器 CLI 自己的版本比它舊、或讀不到自己的版本而 policy 有宣告 → exit 2，
  訊息說明修法。
- 只從 `--policy` 讀：conf.d 裡的 policy 檔寫了這個鍵不影響任何事。

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _grar_findings_cases as cases  # noqa: E402

import _grar_validate as gv  # noqa: E402
import generate_alertmanager_routes as gar  # noqa: E402

OWN = gar.da_tools_version()


def _policy(tmp_path: Path, body: str) -> str:
    p = tmp_path / "policy.yaml"
    p.write_text(body, encoding="utf-8")
    return str(p)


@pytest.mark.parametrize("body,want", [
    ("allowed_domains: []\n", None),
    ('allowed_domains: []\nmin_generator_version: "2.9.0"\n', "2.9.0"),
    ("allowed_domains: []\nmin_generator_version: '10.0.12'\n", "10.0.12"),
])
def test_read_with_allowed_domains(tmp_path, body, want):
    doc = gv.load_policy_document(_policy(tmp_path, body))
    assert doc.min_generator_version == want and doc.allowed_domains == []
    assert gv.load_policy(_policy(tmp_path, body)) == []


@pytest.mark.parametrize("value", [
    "2.9", "2.9.0.1", "v2.9.0", "2.9.0-rc1", "02.9.0", '" 2.9.0"', '""', "",
    "~", "{a: 1}",
])
def test_a_value_that_is_not_x_y_z_makes_the_policy_unusable(tmp_path, value):
    raw = f"allowed_domains: []\nmin_generator_version: {value}\n"
    with pytest.raises(gv.PolicyInputError, match="min_generator_version"):
        gv.load_policy_document(_policy(tmp_path, raw))


@pytest.mark.parametrize("value", ['"2.9"', "2.9", "290", "[2, 9, 0]", '"v2.9.0"'])
def test_quoted_and_unquoted_non_versions_are_refused(tmp_path, value):
    raw = f"allowed_domains: []\nmin_generator_version: {value}\n"
    with pytest.raises(gv.PolicyInputError, match="min_generator_version"):
        gv.load_policy_document(_policy(tmp_path, raw))


@pytest.mark.parametrize("own,minimum,ok", [
    ("2.9.0", None, True),
    ("2.9.0", "2.9.0", True),
    ("2.9.1", "2.9.0", True),
    ("3.0.0", "2.10.0", True),
    ("2.10.0", "2.9.9", True),     # numeric, not string, comparison
    ("2.9.0", "2.9.1", False),
    ("2.9.0", "2.10.0", False),
    ("3.0.0-rc1", "3.0.0", False),  # a pre-release is older than its release
    ("3.0.0-rc1", "2.9.0", True),
    (None, "1.0.0", False),         # own version unreadable
    (None, None, True),
])
def test_version_comparison(own, minimum, ok):
    problem = gv.generator_version_problem(own, minimum)
    assert (problem is None) == ok
    if not ok:
        assert minimum in problem and "da-tools" in problem


def _run(confdir: Path, policy: str) -> subprocess.CompletedProcess:
    return cases.run_gar(confdir, ["--validate", "--strict"],
                         ["--policy", policy])


def test_cli_refuses_when_older_than_the_policy_asks(tmp_path):
    d = cases.build_tree(tmp_path, "clean")
    major = int(OWN.split(".")[0])
    res = _run(d, _policy(tmp_path, f'allowed_domains: []\nmin_generator_version: "{major + 1}.0.0"\n'))
    assert res.returncode == 2
    assert "min_generator_version" in res.stderr and "image tag" in res.stderr


def test_cli_runs_when_new_enough(tmp_path):
    d = cases.build_tree(tmp_path, "clean")
    base = _run(d, _policy(tmp_path, "allowed_domains: []\n"))
    res = _run(d, _policy(tmp_path, f'allowed_domains: []\nmin_generator_version: "{OWN}"\n'))
    assert (res.returncode, res.stdout, res.stderr) == (base.returncode, base.stdout, base.stderr)
    assert base.returncode == 0


def test_cli_malformed_value_is_exit_2(tmp_path):
    d = cases.build_tree(tmp_path, "clean")
    res = _run(d, _policy(tmp_path, "allowed_domains: []\nmin_generator_version: 3\n"))
    assert res.returncode == 2 and "min_generator_version" in res.stderr


def test_cli_unreadable_own_version_refuses(tmp_path, monkeypatch):
    """No VERSION to read: a policy that names a minimum cannot be met."""
    monkeypatch.setattr(gar, "da_tools_version", lambda: None)
    d = cases.build_tree(tmp_path, "clean")
    pol = _policy(tmp_path, 'allowed_domains: []\nmin_generator_version: "1.0.0"\n')
    with pytest.raises(SystemExit) as exc:
        gar.main(["--config-dir", str(d), "--validate", "--strict", "--policy", pol])
    assert exc.value.code == 2


def test_the_key_in_a_confd_policy_file_changes_nothing(tmp_path):
    """Only the deployment --policy is read for it (anyone with write access
    could otherwise make the container unusable)."""
    d = cases.build_tree(tmp_path, "clean")
    base = cases.run_gar(d, ["--validate", "--strict"])
    (d / "_domain_policy.yaml").write_text(
        'min_generator_version: "999.0.0"\ndomain_policies: {}\n', encoding="utf-8")
    res = cases.run_gar(d, ["--validate", "--strict"])
    assert res.returncode == base.returncode == 0
