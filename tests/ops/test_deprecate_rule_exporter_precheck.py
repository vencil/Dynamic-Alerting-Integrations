"""
tests/ops/test_deprecate_rule_exporter_precheck.py — deprecate_rule asks the
exporter before it writes (#1822, D12 = A + C).

`da-guard key-refs` answers, with the exporter's own code, (i) which files of
the tree the exporter's load drops and (ii) which keys the exporter assigns
to the metric being retired (retired alias spellings, `_critical`,
dimensional keys). The tool mirrors none of it.

The two regressions are the shapes measured on main before this change:
  * a tenant writing `mysql_cpu` (the retired alias of
    `mysql_threads_running`) kept it after `--execute`, rc 0 「下架完成」;
  * a root `_defaults.yaml` with `optional_overrides: 5` (the exporter drops
    the whole carrier) previewed rc 0 「✅ 將從 _defaults.yaml 移除」.

Every test but the 未體檢 one runs with the real da-guard (`da_guard_env`);
without it CI would only ever take the unavailable path.
"""

import os
import subprocess
import sys
from pathlib import Path

import deprecate_rule  # noqa: E402

TOOL = Path(deprecate_rule.__file__).resolve()
METRIC = "mysql_threads_running"

_DEFAULTS = "defaults:\n  disk_usage: 80\n  mysql_threads_running: 30\n"


def _tree(root: Path, files: dict[str, str]) -> Path:
    conf_d = root / "conf.d"
    for rel, body in files.items():
        p = conf_d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return conf_d


def _bytes(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _run(conf_d: Path, *extra: str, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), METRIC, "--config-dir", str(conf_d), *extra],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        env={**(os.environ if env is None else env), "PYTHONIOENCODING": "utf-8"})


def _alias_tree(tmp_path: Path) -> Path:
    return _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "t1.yaml": "tenants:\n  t1:\n    mysql_cpu: \"50\"\n",
        "t2.yaml": "tenants:\n  t2:\n    mysql_threads_running: \"40\"\n",
    })


def test_a_tenants_legacy_alias_spelling_is_removed_like_the_canonical_key(
        tmp_path, da_guard_env):
    """Regression 1: on main the tool left t1's `mysql_cpu` and said 下架完成."""
    conf_d = _alias_tree(tmp_path)
    r = _run(conf_d, "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "t1.yaml → [tenants.t1] mysql_cpu: 50" in r.stdout
    assert "已移除: t1.yaml → t1.mysql_cpu" in r.stdout
    assert "mysql_cpu" not in (conf_d / "t1.yaml").read_text(encoding="utf-8")
    assert "mysql_threads_running" not in (conf_d / "t2.yaml").read_text(encoding="utf-8")
    assert "下架完成" in r.stdout
    # The exporter, asked again, finds nothing left.
    after = subprocess.run(
        [da_guard_env, "key-refs", "--config-dir", str(conf_d), "--metric", METRIC],
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=120)
    assert after.returncode == 0, after.stderr
    assert '"mysql_threads_running": []' in after.stdout, after.stdout


def test_every_spelling_the_exporter_assigns_is_cleared_and_nothing_else(
        tmp_path, da_guard_env):
    """`_critical` and dimensional legacy spellings, and a legacy key in the
    carrier, all go; a key that only shares the prefix stays."""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  disk_usage: 80\n  mysql_cpu: 30\n",
        "t1.yaml": ("tenants:\n  t1:\n    mysql_cpu_critical: \"90\"\n"
                    "    mysql_cpu{db='a'}: \"5\"\n    disk_usage: \"70\"\n"),
    })
    r = _run(conf_d, "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (conf_d / "_defaults.yaml").read_text(encoding="utf-8") == (
        "defaults:\n  disk_usage: 80\n")
    assert (conf_d / "t1.yaml").read_text(encoding="utf-8") == (
        "tenants:\n  t1:\n    disk_usage: '70'\n")


def test_a_root_carrier_the_exporter_drops_blocks_preview_and_execute(
        tmp_path, da_guard_env):
    """Regression 2: on main the preview was rc 0 with ✅ on Step 1."""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS + "optional_overrides: 5\n",
        "t2.yaml": "tenants:\n  t2:\n    mysql_threads_running: \"40\"\n",
    })
    before = _bytes(conf_d)

    preview = _run(conf_d)
    assert preview.returncode == 1, preview.stdout + preview.stderr
    assert "✅ 將從 _defaults.yaml" not in preview.stdout
    assert "⛔ 將從 _defaults.yaml 移除 1 個 key——但這份載體讀不進去" in preview.stdout
    tail = preview.stdout.split("下架未完成", 1)[1]
    assert "_defaults.yaml：exporter 讀不進這份檔（da-guard：本輪寫入後仍在 parse_failed" in tail

    execute = _run(conf_d, "--execute")
    assert execute.returncode == 1, execute.stdout + execute.stderr
    assert "本輪降級為預覽" in execute.stdout
    assert _bytes(conf_d) == before


def test_da_guards_stderr_is_passed_on_whole_behind_the_prefix(tmp_path, da_guard_env):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS + "optional_overrides: 5\n",
        "t2.yaml": "tenants:\n  t2:\n    mysql_threads_running: \"40\"\n",
    })
    direct = subprocess.run(
        [da_guard_env, "key-refs", "--config-dir", str(conf_d), "--metric", METRIC],
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=120)
    assert direct.returncode == 3
    want = [ln.rstrip() for ln in direct.stderr.split("\n") if ln.strip()]
    assert len(want) >= 3, want  # the ERROR, its continuation line, the summary
    r = _run(conf_d)
    got = [ln[len(deprecate_rule.DA_GUARD_PREFIX):] for ln in r.stderr.split("\n")
           if ln.startswith(deprecate_rule.DA_GUARD_PREFIX)]
    # The run on the tree as it is comes first, whole; the run on the copy
    # with this round's writes applied follows, whole too (its paths are the
    # copy's).
    assert got[:len(want)] == want, r.stderr
    assert len(got) == 2 * len(want), r.stderr
    assert all(("_defaults.yaml" in a) == ("_defaults.yaml" in b)
               for a, b in zip(want, got[len(want):])), r.stderr


def test_a_carrier_this_round_repairs_is_not_blocked(tmp_path, da_guard_env):
    """The exporter drops the carrier today only because of a key this round
    deletes: judged after the writes, it is fine (the copy, not the tree)."""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  disk_usage: 80\n  mysql_threads_running: disable\n",
        "t2.yaml": "tenants:\n  t2:\n    disk_usage: \"70\"\n",
    })
    r = _run(conf_d, "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (conf_d / "_defaults.yaml").read_text(encoding="utf-8") == (
        "defaults:\n  disk_usage: 80\n")


def test_a_tree_the_exporter_refuses_is_not_written(tmp_path, da_guard_env):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "a.yaml": "tenants:\n  t1:\n    mysql_threads_running: \"40\"\n",
        "b.yaml": "tenants:\n  t1:\n    disk_usage: \"70\"\n",
    })
    before = _bytes(conf_d)
    r = _run(conf_d, "--execute")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "exporter 拒絕載入整棵樹" in r.stdout
    assert _bytes(conf_d) == before


def test_a_tenant_file_the_exporter_drops_is_left_alone_and_named(tmp_path, da_guard_env):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        # t4's body is a list: the exporter's decode refuses the whole file.
        "t3.yaml": "tenants:\n  t3:\n    mysql_threads_running: \"40\"\n  t4: [1]\n",
    })
    before = (conf_d / "t3.yaml").read_bytes()
    r = _run(conf_d, "--execute")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "t3.yaml exporter 讀不進去（da-guard：parse_failed）" in r.stdout
    assert (conf_d / "t3.yaml").read_bytes() == before
    assert "租戶檔 exporter 讀不進去（da-guard：parse_failed）" in r.stdout.split("下架未完成", 1)[1]


def test_without_da_guard_the_run_says_unchecked_and_behaves_as_before(tmp_path):
    """`$DA_GUARD_BINARY` naming no file stops the resolution: 未體檢, the
    tool's own verdict and rc unchanged (D12: never blocks)."""
    conf_d = _alias_tree(tmp_path)
    env = {**os.environ, "DA_GUARD_BINARY": str(tmp_path / "no-such-da-guard")}
    r = _run(conf_d, "--execute", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "未體檢：找不到 da-guard" in r.stdout
    summary = r.stdout.split("=" * 60)[-2]
    assert "未體檢" in summary and "下架完成" in summary
    # Without the exporter's answer the alias spelling is not known to the tool.
    assert "mysql_cpu" in (conf_d / "t1.yaml").read_text(encoding="utf-8")
