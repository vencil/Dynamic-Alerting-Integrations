"""
tests/ops/test_deprecate_rule_carriers.py — deprecate_rule writes EVERY
defaults carrier, and says so only when it did (#1609).

The exporter merges both `_defaults.yaml` and `_defaults.yml` into the
defaults chain (`config_hierarchy.go:216`), while `deprecate_rule --execute`
used to write only the one carrier `resolve_defaults_file` picks — and then
print 「✅ 下架完成」 under a scan that had just listed both. These tests pin
the two halves of the fix: every carrier is written, and the summary may
not claim completion when "found N" and "changed N" disagree.

Driven through `subprocess` because the contract under test is the
operator-visible report (stdout + rc), not a return value.
"""

import os
import subprocess
import sys
from pathlib import Path

import deprecate_rule  # noqa: E402

TOOL = Path(deprecate_rule.__file__).resolve()


def _write(root: Path, name: str, body: str) -> None:
    (root / name).write_text(body, encoding="utf-8")


def _ticket_fixture(root: Path) -> Path:
    """The fixture from #1609: two spellings, deliberately different values."""
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "_defaults.yml", "defaults:\n  cpu_usage: 90\n")
    _write(root, "alpha.yaml", "tenants:\n  alpha:\n    cpu_usage: 85\n")
    return root


def _run(config_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "cpu_usage", "--config-dir",
         str(config_dir), *extra],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})


def _step1_lines(stdout: str) -> list[str]:
    return [ln.strip() for ln in stdout.splitlines()
            if ln.strip().startswith("Step 1:")]


def test_execute_writes_every_carrier_and_only_then_says_complete(tmp_path):
    """(a) The ticket fixture: both spellings end as `disable`, rc 0."""
    root = _ticket_fixture(tmp_path / "conf.d")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert _step1_lines(r.stdout) == ["Step 1: _defaults.yaml",
                                      "Step 1: _defaults.yml"], r.stdout
    assert "下架完成" in r.stdout
    assert "下架未完成" not in r.stdout
    for name in ("_defaults.yaml", "_defaults.yml"):
        text = (root / name).read_text(encoding="utf-8")
        assert text.rstrip().endswith("cpu_usage: disable"), (
            f"{name} still carries the threshold:\n{text}")
    alpha = (root / "alpha.yaml").read_text(encoding="utf-8")
    assert "cpu_usage" not in alpha, alpha


def test_preview_names_both_carriers_and_writes_nothing(tmp_path):
    """(b) Preview mode names every carrier it WOULD write, rc 0, no writes."""
    root = _ticket_fixture(tmp_path / "conf.d")
    before = {p.name: p.read_text(encoding="utf-8") for p in root.iterdir()}

    r = _run(root)

    assert r.returncode == 0, r.stdout + r.stderr
    assert _step1_lines(r.stdout) == ["Step 1: _defaults.yaml",
                                      "Step 1: _defaults.yml"], r.stdout
    assert "預覽模式" in r.stdout
    after = {p.name: p.read_text(encoding="utf-8") for p in root.iterdir()}
    assert after == before, "preview mode wrote to the tree"


def test_an_unwritable_carrier_blocks_the_completion_claim(tmp_path):
    """(c) One carrier cannot be read → rc 1, named, and NOT 下架完成.

    ⚠️ The unwritable shape is a broken YAML document, not mode bits: the
    suite runs as root, where `chmod` denies nothing. A carrier the scan
    skips with 無法讀取 is exactly the shape that used to vanish between
    「發現 N 處引用」 and 「下架完成」.

    The other carrier's write must survive: one carrier's failure is a
    reason to withhold the completion claim, not to undo the work done.
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "_defaults.yml", "defaults: [\n")
    _write(root, "alpha.yaml", "tenants:\n  alpha:\n    cpu_usage: 85\n")

    r = _run(root, "--execute")

    assert r.returncode == 1, r.stdout + r.stderr
    assert "下架未完成" in r.stdout, r.stdout
    assert "下架完成！" not in r.stdout, r.stdout
    tail = r.stdout.split("下架未完成", 1)[1]
    assert "_defaults.yml" in tail, tail
    assert "無法讀取" in tail, tail
    text = (root / "_defaults.yaml").read_text(encoding="utf-8")
    assert text.rstrip().endswith("cpu_usage: disable"), text


def test_single_carrier_control_keeps_todays_shape(tmp_path):
    """(d) One `_defaults.yaml` → one Step 1 line, 下架完成, rc 0."""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "alpha.yaml", "tenants:\n  alpha:\n    cpu_usage: 85\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert _step1_lines(r.stdout) == ["Step 1: _defaults.yaml"], r.stdout
    assert "下架完成" in r.stdout
    assert "下架未完成" not in r.stdout
    text = (root / "_defaults.yaml").read_text(encoding="utf-8")
    assert text.rstrip().endswith("cpu_usage: disable"), text


def test_no_carrier_control_names_the_canonical_path_and_is_incomplete(
        tmp_path):
    """(e) No defaults carrier at all → Step 1 names `_defaults.yaml`,
    reports 不存在, and the run is incomplete (rc 1).

    Rule: a Step 1 result that is not ok and was not in the scan's own
    findings is still a carrier the tool could not write, so the summary
    may not read as clean. Before #1609 this tree printed 「✅ 下架完成」
    over a ❌ Step 1 — the same contradiction as the two-carrier case.
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "alpha.yaml", "tenants:\n  alpha:\n    cpu_usage: 85\n")

    r = _run(root, "--execute")

    assert _step1_lines(r.stdout) == ["Step 1: _defaults.yaml"], r.stdout
    assert "_defaults.yaml 不存在" in r.stdout, r.stdout
    assert r.returncode == 1, r.stdout + r.stderr
    assert "下架未完成" in r.stdout, r.stdout
    assert "下架完成！" not in r.stdout, r.stdout
    # Step 2 still ran: the tenant override was removed.
    alpha = (root / "alpha.yaml").read_text(encoding="utf-8")
    assert "cpu_usage" not in alpha, alpha


def test_defaults_carriers_returns_exact_names_only_sorted(tmp_path):
    """(f) Exact-name carriers in any casing; prefix cousins, tenants and
    directories are not carriers."""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults: {}\n")
    _write(root, "_DEFAULTS.YML", "defaults: {}\n")
    _write(root, "_defaults-multidb.yaml", "defaults: {}\n")
    _write(root, "alpha.yaml", "tenants: {}\n")
    (root / "sub").mkdir()
    _write(root / "sub", "_defaults.yml", "defaults: {}\n")

    got = deprecate_rule.defaults_carriers(root)

    assert got == [root / "_DEFAULTS.YML", root / "_defaults.yaml"], got


def test_non_carrier_files_with_a_defaults_block_follow_the_exporter_rule(
        tmp_path):
    """Blind review: `found` was every file with a `defaults:` block, so a
    tenant file carrying one made the run rc 1 with reason 未寫入 — and no
    rerun could clear it. The exporter's own rule decides: a tenant file's
    `defaults:` is dropped (WARN) → named, not counted; a `_`-prefixed
    non-carrier IS merged in flat mode but this tool may not write it →
    incomplete, by name, with a reason that says so."""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root, "alpha.yaml",
           "defaults:\n  cpu_usage: 70\ntenants:\n  alpha:\n    cpu_usage: 85\n")

    r = _run(root, "--execute")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "下架完成" in r.stdout
    assert "alpha.yaml 的 defaults 區塊 exporter 會丟棄" in r.stdout, r.stdout
    assert "未寫入" not in r.stdout, r.stdout

    root2 = tmp_path / "conf.d2"
    root2.mkdir()
    _write(root2, "_defaults.yaml", "defaults:\n  cpu_usage: 80\n")
    _write(root2, "_defaults-multidb.yaml", "defaults:\n  cpu_usage: 60\n")

    r = _run(root2, "--execute")

    assert r.returncode == 1, r.stdout + r.stderr
    tail = r.stdout.split("下架未完成", 1)[1]
    assert "_defaults-multidb.yaml" in tail and "非 defaults 載體" in tail, tail
    assert "未寫入" not in tail, tail
    assert (root2 / "_defaults.yaml").read_text(encoding="utf-8").rstrip().endswith(
        "cpu_usage: disable")


def test_a_carrier_whose_defaults_key_is_null_does_not_traceback(tmp_path):
    """Blind review: `defaults:` with no value parses to None; `pk in None`
    raised TypeError → bare traceback, rc 1 — the code the docs now reserve
    for 下架未完成. It is an empty defaults block and is written like one."""
    root = tmp_path / "conf.d"
    root.mkdir()
    _write(root, "_defaults.yaml", "# header\ndefaults:\n")

    r = _run(root, "--execute")

    assert "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 0, r.stdout + r.stderr
    text = (root / "_defaults.yaml").read_text(encoding="utf-8")
    assert text.startswith("# header\n"), text
    assert text.rstrip().endswith("cpu_usage: disable"), text
