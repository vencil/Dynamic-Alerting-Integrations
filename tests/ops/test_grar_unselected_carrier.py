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
    # warn_multi_carrier's own line is not printed besides (one fact, once).
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
