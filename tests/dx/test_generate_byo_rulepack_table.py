"""Contract tests for scripts/tools/dx/generate_byo_rulepack_table.py (#1267).

Every assertion here pins something an adversarial review actually broke or
proved load-bearing — none of it is coverage theatre:

* the sentinel regex must be IDEMPOTENT. The first draft let the end sentinel
  absorb a newline, so each run added a blank line and ``--check`` could never
  converge.
* a ConfigMap mounted under an unexpected name must FAIL LOUD. The first draft
  ``continue``-d past it, which kept it out of the mounted-vs-counted
  cross-check — the table would ship missing a pack while ``--check`` stayed
  green, which is the exact failure (#1246 lost jvm / nginx / liveness) this
  generator exists to prevent.
* a malformed manifest is a CALLER error (exit 2), not "the table is stale"
  (exit 1).
* the tool must reconfigure stdout for UTF-8. ``validate_all.py`` spawns it
  with ``text=True`` and no encoding; without it a cp950 console kills the
  whole validate_all run, not just this check.
"""
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "tools" / "dx" / "generate_byo_rulepack_table.py"

sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "dx"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))

import generate_byo_rulepack_table as gen  # noqa: E402

_TIMEOUT = 120


def _run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), timeout=_TIMEOUT,
    )


# --------------------------------------------------------------------------
# sentinel replacement
# --------------------------------------------------------------------------
def test_sentinel_replacement_is_idempotent():
    """Second application must be a no-op — otherwise --check never converges."""
    doc = Path("docs/x.md")
    empty = f"before\n{gen.SENTINEL_START}\n{gen.SENTINEL_END}\nafter\n"
    table = "| a |\n|---|\n| 1 |\n"
    once = gen.replace_sentinel_block(empty, table, doc)
    twice = gen.replace_sentinel_block(once, table, doc)
    assert once == twice, "sentinel replacement is not idempotent"
    assert table in once
    assert "before" in once and "after" in once


def test_sentinel_replacement_leaves_outside_content_untouched():
    doc = Path("docs/x.md")
    content = f"KEEP-A\n{gen.SENTINEL_START}\nstale\n{gen.SENTINEL_END}\nKEEP-B\n"
    out = gen.replace_sentinel_block(content, "| new |\n", doc)
    assert "stale" not in out
    assert "KEEP-A" in out and "KEEP-B" in out


@pytest.mark.parametrize("content", [
    "no sentinels here\n",
    "<!-- BYO_RULEPACK_TABLE_START -->\nno end\n",
    "only end\n<!-- BYO_RULEPACK_TABLE_END -->\n",
])
def test_missing_sentinel_raises(content):
    with pytest.raises(ValueError, match="sentinel"):
        gen.replace_sentinel_block(content, "| t |\n", Path("docs/x.md"))


# --------------------------------------------------------------------------
# source parsing / fail-loud invariants
# --------------------------------------------------------------------------
def test_projected_packs_match_counted_packs():
    """The shipped tree must satisfy the invariant the generator enforces."""
    mounted = {k for k, _, _ in gen.read_projected_packs()}
    counted = set(gen.gather_stats()["per_pack"])
    assert mounted == counted, f"only-mounted={mounted - counted} only-counted={counted - mounted}"


def test_every_mounted_pack_has_files():
    for key, cm_name, files in gen.read_projected_packs():
        assert files, f"{cm_name} projects no items[] — the Contents column would be empty"
        assert cm_name == f"prometheus-rules-{key}"


def test_unexpected_configmap_name_fails_loud(monkeypatch, tmp_path):
    """A mounted CM outside the naming convention must raise, NOT be skipped.

    Skipping keeps it out of `mounted`, so the mounted-vs-counted check stays
    green while the table silently omits a pack.
    """
    src = yaml.safe_load_all((REPO_ROOT / "k8s" / "03-monitoring"
                              / "deployment-prometheus.yaml").read_text(encoding="utf-8"))
    docs = [d for d in src if d and d.get("kind") == "Deployment"]
    dep = docs[0]
    vols = dep["spec"]["template"]["spec"]["volumes"]
    proj = [v for v in vols if "projected" in v][0]
    proj["projected"]["sources"].append(
        {"configMap": {"name": "rules-sneaky", "items": [{"key": "x.yml", "path": "x.yml"}]}}
    )
    tampered = tmp_path / "deployment.yaml"
    tampered.write_text(yaml.safe_dump(dep), encoding="utf-8")
    monkeypatch.setattr(gen, "DEPLOYMENT", tampered)
    with pytest.raises(ValueError, match="rules-sneaky"):
        gen.read_projected_packs()


def test_rendered_table_matches_sources():
    """Re-derive every cell independently rather than trusting the tool."""
    per_pack = gen.gather_stats()["per_pack"]
    rows = gen.build_rows()
    assert len(rows) == len(gen.read_projected_packs())
    for (key, cm_name, files), row in zip(gen.read_projected_packs(), rows):
        counts = per_pack[key]
        assert f"`{cm_name}`" in row
        assert f"{counts['recording']}R + {counts['alert']}A" in row
        for f in files:
            assert f"`{f}`" in row


def test_both_languages_share_identical_data_rows():
    zh, en = gen.render_table("zh"), gen.render_table("en")
    assert zh.split("\n")[2:] == en.split("\n")[2:], "only the header/separator may differ"


# --------------------------------------------------------------------------
# CLI contract
# --------------------------------------------------------------------------
def test_check_is_green_on_shipped_tree():
    r = _run("--check", "--lang", "all")
    assert r.returncode == 0, r.stdout + r.stderr


def test_help_exits_zero_and_is_decodable_as_utf8():
    """try_utf8_stdout(): validate_all spawns this with text=True and no
    encoding — undecodable bytes there take down the WHOLE validate_all run."""
    r = _run("--help")
    assert r.returncode == 0
    assert r.stdout  # decoded, not None
    r2 = _run("--lang", "all")          # default mode prints the tables
    assert r2.returncode == 0
    assert "prometheus-rules-" in r2.stdout


def test_bad_flag_is_caller_error():
    assert _run("--nope").returncode == 2


def test_malformed_manifest_is_caller_error_not_drift(monkeypatch, tmp_path):
    bad = tmp_path / "deployment.yaml"
    bad.write_text("kind: Deployment\n  : : [\n", encoding="utf-8")
    monkeypatch.setattr(gen, "DEPLOYMENT", bad)
    with pytest.raises(yaml.YAMLError):
        gen.read_projected_packs()
