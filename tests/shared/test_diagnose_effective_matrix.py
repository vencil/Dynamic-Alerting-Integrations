"""`diagnose --show-inheritance` 的答案要與 Go 的 da-guard effective 一致（#2526、#2547）。

step 0 的政策：Python 工具不自己解讀 YAML 語意，值一律來自 Go。diagnose 原本用 PyYAML 重讀
conf.d，於是 PyYAML 與 yaml.v3 讀法不同的地方都分歧。修正前（main 485ddf4d）實測：

- 租戶值 `2024-02-30`（不存在的日期）：diagnose 整支 traceback、rc 1；Go 照原文給值。
- 租戶值 `!foo 70`（未知 tag）：diagnose 整份租戶檔跳過、回 defaults；Go 忽略 tag 照讀。
- 租戶值 `2001-12-14<TAB>01:02:03`、`!!str [5]`：diagnose 整份檔跳過；Go 照讀。
- `tenants: !!set {tx}` 加平台檔 `_profile: gold`：diagnose 說「沒有租戶檔宣告 tx」、profile
  None；Go 認得 tx、綁 gold。帶值的 `!!set {tx: {...}}` 同樣。
- `_defaults.yaml` 的鍵 `~: 5`：diagnose 印 `null`，Go 是 `<nil>`；`1: 5`、`true: 5` 同類。
- 對照組（合法值、`tenants: {tx: {}}`、一般字串鍵）兩邊本來就一致。

每格比對：結束碼為 0、`resolved`（effective_config 去掉 `_` 開頭的鍵）、`profile_name`
（Go 綁到的 profile），以及 chain 把每個 key 歸在哪一層（Go 的 key_sources）。

da-guard 由 conftest 的 session fixture 以 `go build` 建出；建不起來就 fail、不 skip。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _lib_tenant_values as tv

REPO_ROOT = Path(__file__).resolve().parents[2]
DIAGNOSE = REPO_ROOT / "scripts" / "tools" / "ops" / "diagnose.py"

pytestmark = pytest.mark.usefixtures("da_guard_env")

_DEFAULTS = "defaults:\n  mysql_connections: 80\n  mysql_slow: 90\n"
_GOLD = "profiles:\n  gold:\n    mysql_slow: 60\n"
_PLATFORM_GOLD = "tenants:\n  tx: {_profile: gold}\n"

# (name, {file: body}); the tenant is always `tx`.
SHAPES = [
    # #2526 — values PyYAML cannot construct
    ("value-date-CONTROL", {"_defaults.yaml": _DEFAULTS,
                            "tx.yaml": "tenants:\n  tx:\n    mysql_connections: 70\n"
                                       "    mysql_slow: 60\n"}),
    ("value-invalid-date", {"_defaults.yaml": _DEFAULTS,
                            "tx.yaml": "tenants:\n  tx:\n    mysql_connections: 2024-02-30\n"
                                       "    mysql_slow: 60\n"}),
    ("value-unknown-tag", {"_defaults.yaml": _DEFAULTS,
                           "tx.yaml": "tenants:\n  tx:\n    mysql_connections: !foo 70\n"
                                      "    mysql_slow: 60\n"}),
    ("value-tab-timestamp", {"_defaults.yaml": _DEFAULTS,
                             "tx.yaml": "tenants:\n  tx:\n    mysql_connections: "
                                        "2001-12-14\t01:02:03\n    mysql_slow: 60\n"}),
    ("value-str-tag-on-sequence", {"_defaults.yaml": _DEFAULTS,
                                   "tx.yaml": "tenants:\n  tx:\n    mysql_connections: !!str [5]\n"
                                              "    mysql_slow: 60\n"}),
    # #2547 item 1 — which tenant exists, and the profile it is bound to
    ("tenants-dict-CONTROL", {"_defaults.yaml": _DEFAULTS, "_platform.yaml": _PLATFORM_GOLD,
                              "_profiles.yaml": _GOLD, "tx.yaml": "tenants:\n  tx: {}\n"}),
    ("tenants-set", {"_defaults.yaml": _DEFAULTS, "_platform.yaml": _PLATFORM_GOLD,
                     "_profiles.yaml": _GOLD, "tx.yaml": "tenants: !!set {tx}\n"}),
    ("tenants-set-with-values", {"_defaults.yaml": _DEFAULTS, "_platform.yaml": _PLATFORM_GOLD,
                                 "_profiles.yaml": _GOLD,
                                 "tx.yaml": "tenants: !!set {tx: {mysql_connections: 71}}\n"}),
    # #2547 item 2 — non-string keys of `defaults:`
    ("defaults-key-CONTROL", {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  k1: 5\n",
                              "tx.yaml": "tenants:\n  tx:\n    k1: 7\n"}),
    ("defaults-key-int", {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  1: 5\n",
                          "tx.yaml": "tenants:\n  tx:\n    1: 7\n"}),
    ("defaults-key-null", {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  ~: 5\n",
                           "tx.yaml": "tenants:\n  tx: {}\n"}),
    ("defaults-key-bool", {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  true: 5\n",
                           "tx.yaml": "tenants:\n  tx: {}\n"}),
]


def _tree(root: Path, files: dict[str, str]) -> Path:
    conf_d = root / "conf.d"
    conf_d.mkdir(parents=True)
    for name, body in files.items():
        (conf_d / name).write_text(body, encoding="utf-8")
    return conf_d


def _diagnose(conf_d: Path) -> dict:
    p = subprocess.run([sys.executable, str(DIAGNOSE), "tx", "--config-dir", str(conf_d),
                        "--show-inheritance"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=120, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    doc = json.loads(p.stdout) if p.returncode == 0 else {}
    return {
        "rc": p.returncode,
        "resolved": doc.get("resolved"),
        "profile": doc.get("profile_name"),
        "layers": {k: c["layer"] for c in doc.get("chain", []) for k in c["keys"]},
    }


def _oracle(conf_d: Path) -> dict:
    t = tv.load_effective(conf_d)["tx"]
    resolved = {k: v for k, v in t.effective_config.items() if not k.startswith("_")}
    return {
        "rc": 0,
        "resolved": resolved,
        "profile": t.profile,
        "layers": {k: t.key_sources[k].layer for k in resolved},
    }


def test_matrix_is_not_vacuous(tmp_path: Path) -> None:
    """Both answers occur on the Go side — bound and unbound, a value as
    written and the defaults — so equal readings mean something."""
    names = [n for n, _ in SHAPES]
    assert len(names) == len(set(names))
    got = {}
    for name, files in SHAPES:
        got[name] = _oracle(_tree(tmp_path / name, files))
    assert {g["profile"] for g in got.values()} == {None, "gold"}
    assert got["value-invalid-date"]["resolved"]["mysql_connections"] == "2024-02-30"
    assert got["tenants-set"]["resolved"] == {"mysql_connections": 80, "mysql_slow": 60}
    assert "<nil>" in got["defaults-key-null"]["resolved"]


@pytest.mark.parametrize("name,files", SHAPES, ids=[n for n, _ in SHAPES])
def test_diagnose_agrees_with_da_guard_effective(tmp_path: Path, name: str,
                                                 files: dict[str, str]) -> None:
    conf_d = _tree(tmp_path, files)
    assert _diagnose(conf_d) == _oracle(conf_d), name
