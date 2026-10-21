"""#2906：一個目錄有兩個 defaults carrier 拼法時，沒被選中的那份。

`select_defaults_carrier` 只選一份；其餘可讀的那份**沒有任何平面讀**
（da-guard parse-tree 的 `not_read` reason `unselected_defaults_carrier`）。
產生器把它當成一筆 WARN finding（同名 kind），`--validate`（含 `--strict`）
擋、render 照常產出 rc 0——比照 `routing_key_ignored`「寫了但不生效」那一類。

產生器**不開**這份檔：裡面寫了 `_routing_enforced`、或根本不是合法 YAML，
結果都一樣。#2906 之前，子目錄的未選拼法若含 `_routing_enforced` 會被讀出來
判 `routing_enforced_below_root`（rc 2），其他情況只有 stderr 一行 WARN、rc 0。

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"

EXIT_OK, EXIT_VIOLATION = 0, 1
KIND = "unselected_defaults_carrier"

_ROOT_RD = ("defaults:\n  mysql_connections: 80\n"
            "_routing_defaults:\n  receiver:\n    type: webhook\n"
            "    url: https://hooks.example.com/root\n")
_SUB_RD = ("_routing_defaults:\n  receiver:\n    type: slack\n"
           "    api_url: https://hooks.slack.com/services/T/B/x\n")

# What the unselected spelling carries. None of it may change the result.
_BODIES = {
    "plain": "defaults:\n  mysql_connections: 10\n",
    "enforced": "_routing_enforced:\n  enabled: true\n_routing_defaults:\n  receiver: null\n",
    "broken_yaml": "a: [1, 2\n",
}


def _tenant(tid: str) -> str:
    return f"tenants:\n  {tid}:\n    mysql_connections: \"70\"\n"


def _tree(root: Path, where: str, body: str) -> tuple[Path, str, str]:
    """A tree whose *where* (``root`` / ``sub``) directory has both spellings;
    returns (conf.d, the unselected file, the selected file)."""
    if where == "root":
        files = {"_defaults.yaml": _ROOT_RD, "_defaults.yml": body,
                 "t-a.yaml": _tenant("t-a")}
        unselected, chosen = "_defaults.yml", "_defaults.yaml"
    else:
        files = {"_defaults.yaml": _ROOT_RD, "team/_defaults.yaml": _SUB_RD,
                 "team/_defaults.yml": body, "team/t-b.yaml": _tenant("t-b")}
        unselected, chosen = "team/_defaults.yml", "team/_defaults.yaml"
    d = root / "conf.d"
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return d, unselected, chosen


def _gar(d: Path, out: Path, *argv: str) -> tuple[subprocess.CompletedProcess, list[dict]]:
    res = subprocess.run(
        [sys.executable, "-s", str(_GAR), "--config-dir", str(d),
         "--findings-json", str(out), *argv],
        capture_output=True, text=True, encoding="utf-8", timeout=180)
    doc = json.loads(out.read_text(encoding="utf-8"))
    return res, doc["findings"]


def _mine(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["kind"] == KIND]


@pytest.mark.parametrize("body", sorted(_BODIES))
@pytest.mark.parametrize("where", ["root", "sub"])
def test_render_produces_output_and_names_the_unselected_carrier(tmp_path, where, body):
    d, unselected, chosen = _tree(tmp_path, where, _BODIES[body])
    res, findings = _gar(d, tmp_path / "f.json", "--dry-run")
    assert res.returncode == EXIT_OK, res.stderr
    assert "receiver: tenant-t-" in res.stdout, res.stdout  # rendered
    [f] = _mine(findings)
    assert (f["file"], f["severity"], f["blocks"]) == (unselected, "warning", "validate"), f
    # The line names the file, the one that IS read, and the fix — once.
    line = f["message"]
    assert line.startswith(f"WARN: {unselected} is not read:"), line
    assert f"only {chosen} is read" in line and "merge them into one file" in line, line
    assert res.stderr.count(line) == 1, res.stderr
    # No second, exporter-worded multi-carrier line besides (one fact, once).
    assert "defaults carriers (" not in res.stderr, res.stderr
    # Not opened: nothing it carries becomes a finding of its own.
    assert not [x for x in findings if x["kind"] != KIND and x.get("file") == unselected], findings


@pytest.mark.parametrize("strict", [False, True], ids=["validate", "validate_strict"])
@pytest.mark.parametrize("body", sorted(_BODIES))
@pytest.mark.parametrize("where", ["root", "sub"])
def test_validate_fails_on_it(tmp_path, where, body, strict):
    d, unselected, _chosen = _tree(tmp_path, where, _BODIES[body])
    res, findings = _gar(d, tmp_path / "f.json", "--validate", *(["--strict"] if strict else []))
    assert res.returncode == EXIT_VIOLATION, res.stdout + res.stderr
    assert "FAIL: 1 error(s) found" in res.stderr, res.stderr
    [f] = _mine(findings)
    assert f["file"] == unselected
    # The only finding about that file: its content was never read, so
    # `_routing_enforced` below the root is no routing_enforced_below_root
    # here (#2906; rc 2 before) and broken YAML no tenant_file_unreadable.
    assert [x["kind"] for x in findings if x.get("file") == unselected] == [KIND], findings


@pytest.mark.parametrize("where", ["root", "sub"])
def test_one_carrier_is_no_finding(tmp_path, where):
    d, unselected, _chosen = _tree(tmp_path, where, "")
    (d / unselected).unlink()
    res, findings = _gar(d, tmp_path / "f.json", "--validate", "--strict")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert not _mine(findings), findings


# ── which spelling is the carrier: the generator's answer is da-guard's ──

sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
from _grar_parse import load_tenant_tree  # noqa: E402
from _platform_fs import symlink_or_skip  # noqa: E402


def _guard_parse_tree(binary: str, d: Path) -> dict:
    res = subprocess.run([binary, "parse-tree", "--config-dir", str(d)],
                         capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _guard_unselected(doc: dict) -> list[str]:
    return sorted(n["file"] for n in doc["not_read"] if n["reason"] == KIND)


@pytest.mark.parametrize("where", ["root", "sub"])
def test_case_variant_selection_is_the_exporters(tmp_path, where, da_guard_binary):
    """`_Defaults.YAML` beside `_defaults.yml`: the folded `.yaml` is the
    carrier (as in Go), the `.yml` is the finding."""
    d = tmp_path / "conf.d"
    sub = "" if where == "root" else "team/"
    files = {"_defaults.yaml": _ROOT_RD} if sub else {}
    files.update({f"{sub}_Defaults.YAML": _ROOT_RD if not sub else _SUB_RD,
                  f"{sub}_defaults.yml": _BODIES["enforced"],
                  f"{sub}t-a.yaml": _tenant("t-a")})
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text, encoding="utf-8")
    doc = _guard_parse_tree(da_guard_binary, d)
    assert doc["defaults_carriers"][sub.rstrip("/") or "."] == f"{sub}_Defaults.YAML", doc
    tree = load_tenant_tree(str(d))
    assert [f.file for f in tree.unselected_carriers] == _guard_unselected(doc) == [
        f"{sub}_defaults.yml"]
    res, findings = _gar(d, tmp_path / "f.json", "--validate")
    assert res.returncode == EXIT_VIOLATION, res.stdout + res.stderr
    assert [f["file"] for f in _mine(findings)] == [f"{sub}_defaults.yml"]
    assert f"only {sub}_Defaults.YAML is read" in _mine(findings)[0]["message"]


def _unusable_yaml_tree(tmp_path: Path, how: str) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yml").write_text(_ROOT_RD, encoding="utf-8")
    (d / "t-a.yaml").write_text(_tenant("t-a"), encoding="utf-8")
    if how == "dangling":
        symlink_or_skip("missing.yaml", d / "_defaults.yaml")
    else:
        (d / "_defaults.yaml").write_text("a: [1, 2\n", encoding="utf-8")
    return d


def test_a_dangling_yaml_is_no_candidate(tmp_path, da_guard_binary):
    """A `_defaults.yaml` that cannot be opened is not a candidate (Go: not
    a carrier either): the readable `.yml` is selected, so nothing is
    unselected, and the dangling link is the unreadable-file refusal."""
    d = _unusable_yaml_tree(tmp_path, "dangling")
    doc = _guard_parse_tree(da_guard_binary, d)
    assert doc["defaults_carriers"]["."] == "_defaults.yml", doc
    tree = load_tenant_tree(str(d))
    assert tree.unselected_carriers == [] and _guard_unselected(doc) == []
    assert [f for f, _ in tree.tenant_file_errors] == ["_defaults.yaml"]
    res, findings = _gar(d, tmp_path / "f.json", "--validate")
    assert res.returncode == EXIT_VIOLATION, res.stdout + res.stderr
    assert not _mine(findings)
    assert [f["file"] for f in findings if f["kind"] == "tenant_file_unreadable"] == ["_defaults.yaml"]


def test_a_broken_yaml_is_still_the_selected_carrier(tmp_path, da_guard_binary):
    """A `_defaults.yaml` that opens but does not parse IS the carrier — the
    selection is by name over the files that can be read, in Go as here — so
    the readable `.yml` beside it is the unselected one. The broken carrier
    refuses the run (tenant_file_unreadable, rc 1), whose document holds the
    refusal only; the finding is in the tree all the same."""
    d = _unusable_yaml_tree(tmp_path, "broken")
    doc = _guard_parse_tree(da_guard_binary, d)
    assert doc["defaults_carriers"]["."] == "_defaults.yaml", doc
    tree = load_tenant_tree(str(d))
    assert [f.file for f in tree.unselected_carriers] == _guard_unselected(doc) == ["_defaults.yml"]
    assert [f for f, _ in tree.tenant_file_errors] == ["_defaults.yaml"]
    res, findings = _gar(d, tmp_path / "f.json", "--validate")
    assert res.returncode == EXIT_VIOLATION, res.stdout + res.stderr
    assert {f["kind"] for f in findings} == {"tenant_file_unreadable", "refusal_summary"}, findings


# ── tools that do not print the generator's stream still name it (#2906) ──

_OPS = REPO / "scripts" / "tools" / "ops"


@pytest.mark.parametrize("argv", [
    ["notification_tester.py", "--dry-run"],
    ["explain_route.py", "--tenant", "t-b"],
    # The line is printed before the mode splits: no amtool needed, the
    # trace's own verdict is not what is asserted.
    ["explain_route.py", "--tenant", "t-b", "--trace"],
], ids=["notification_tester", "explain_route", "explain_route_trace"])
def test_reader_warnings_are_printed_once(tmp_path, argv):
    """`print_reader_warnings`: the line each unselected carrier gets — the
    reader's own multi-carrier WARN before #2906, so every caller showed it."""
    d, unselected, _chosen = _tree(tmp_path, "sub", _BODIES["plain"])
    (d / "_defaults.yml").write_text(_BODIES["plain"], encoding="utf-8")
    tool, *rest = argv
    res = subprocess.run([sys.executable, "-s", str(_OPS / tool), *rest, "--config-dir", str(d)],
                         capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert "Traceback" not in res.stderr, res.stderr
    for f in ("_defaults.yml", unselected):
        assert res.stderr.count(f"WARN: {f} is not read:") == 1, res.stderr
