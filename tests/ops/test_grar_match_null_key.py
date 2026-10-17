"""#2504 → ADR-036 步驟 4（#2906）：null key（`~:` / `null:` / 空 key）讓整份檔讀不了。

#2504 當時的缺陷：PyYAML 讀法在每一層 mapping 都丟 null key，於是
`routes[i].match: {~: x, severity: critical}` 在產生器眼中只剩
`severity: critical`——route 變寬（fail-open）。那時的修法是在 route match 的
結構路徑上把 null key 放回。

步驟 4 起產生器改讀 `da-guard parse-tree`：null key 是 parse-tree 封閉清單裡的
`null_key`（ADR-036 §2），檔案**整份**讀不了。釘住的契約：

* 不論 null key 寫在哪裡（route match、`_metadata`、租戶層、routing profile），
  也不論哪一種拼法，產生器都把該檔列為讀不了、帶 `null_key`，並拒收整棵樹
  （`--validate --strict` 回 1）；route 不會變寬，因為根本不渲染。
* 加了引號的 `"~":` 是普通的鍵，照讀。
* `_lib_confd.declared_tenant_ids` 也讀 parse-tree：這種檔不宣告任何租戶。

Fixture 的 tenant 名一律是 demo 名（tenant-agnostic）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from generate_alertmanager_routes import load_tenant_tree
from _lib_confd import declared_tenant_ids

REPO = Path(__file__).resolve().parents[2]
_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"

_T = "demo-apac"

_TENANT_FILE = """\
tenants:
  {tenant}:
    _routing:
      receiver:
        type: webhook
        url: https://hooks.example.com/main
      routes:
      - match:
{match}
        receiver:
          type: pagerduty
          service_key: k
"""

# Every spelling yaml.v3 resolves to a null key.
_NULL_KEYS = ["~", "null", "Null", "NULL", '!!null ""', "? "]


def _match(key: str, *more: str) -> str:
    pad = " " * 10
    first = f"{pad}{key}: x" if key != "? " else f"{pad}? \n{pad}: x"
    return "\n".join([first, *(f"{pad}{m}" for m in more)])


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir(parents=True)
    for name, text in files.items():
        (d / name).write_text(text, encoding="utf-8")
    return d


def _gar(d: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_GAR), "--config-dir", str(d), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300)


@pytest.mark.parametrize("key", _NULL_KEYS)
def test_null_key_in_a_route_match_makes_the_file_unreadable(tmp_path, key):
    d = _tree(tmp_path, {f"{_T}.yaml": _TENANT_FILE.format(
        tenant=_T, match=_match(key, "severity: critical"))})
    tree = load_tenant_tree(str(d))
    assert [f for f, _r in tree.tenant_file_errors] == [f"{_T}.yaml"]
    assert "(null_key)" in tree.tenant_file_errors[0][1]
    assert _T not in tree.routing_configs
    r = _gar(d, "--validate", "--strict")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "null_key" in r.stderr


@pytest.mark.parametrize("where", [
    # A null key anywhere in the file, not only where a match is read.
    f"tenants:\n  {_T}:\n    _metadata:\n      ~: x\n      owner: sre\n",
    f"tenants:\n  {_T}:\n    ~: x\n    cpu_usage_percent: '80'\n",
    "~: x\ntenants:\n  " + _T + ": {}\n",
])
def test_null_key_anywhere_refuses_the_file(tmp_path, where):
    d = _tree(tmp_path, {f"{_T}.yaml": where})
    tree = load_tenant_tree(str(d))
    assert [f for f, _r in tree.tenant_file_errors] == [f"{_T}.yaml"]
    assert "(null_key)" in tree.tenant_file_errors[0][1]


def test_null_key_in_a_routing_profile_refuses_the_profiles_file(tmp_path):
    d = _tree(tmp_path, {
        "_routing_profiles.yaml": ("routing_profiles:\n  p1:\n    receiver:\n      type: webhook\n"
                                   "      url: https://hooks.example.com/p\n    routes:\n"
                                   "    - match:\n        ~: x\n      receiver:\n"
                                   "        type: pagerduty\n        service_key: k\n"),
        f"{_T}.yaml": f"tenants:\n  {_T}:\n    _routing_profile: p1\n",
    })
    tree = load_tenant_tree(str(d))
    assert [f for f, _r in tree.tenant_file_errors] == ["_routing_profiles.yaml"]
    assert _gar(d, "--validate", "--strict").returncode == 1


def test_a_quoted_tilde_is_a_plain_key(tmp_path):
    d = _tree(tmp_path, {f"{_T}.yaml": _TENANT_FILE.format(
        tenant=_T, match=_match('"~"', "severity: critical"))})
    tree = load_tenant_tree(str(d))
    assert tree.tenant_file_errors == []
    assert tree.routing_configs[_T]["routes"][0]["match"] == {"~": "x", "severity": "critical"}


def test_a_file_with_a_null_key_declares_no_tenant(tmp_path):
    d = _tree(tmp_path, {
        f"{_T}.yaml": f"tenants:\n  {_T}:\n    ~: x\n",
        "demo-emea.yaml": "tenants:\n  demo-emea: {}\n",
    })
    assert declared_tenant_ids(str(d)) == {"demo-emea"}
