"""#3489 第一步：共用鍵判決的 ERROR 層移植到 Go（`da-guard key-verdict`），
本檔釘住 Go == Python。

比對的不是另寫的一份 fixture，而是 Python 判決自己的測試所建的每一棵樹：
`test_key_verdict.py` 與 `test_membership_matrix_verdict.py`（走過
`optional_overrides_membership_matrix.json` 每一列）照常執行，每次
`judge_tree(...)` 成功回來時，就在同一棵樹、同一個時刻（`at`）跑
`da-guard key-verdict`，比對兩邊「租戶的鍵讀成 unread、等級 ERROR」的集合
（tenant, key, reason, file）。兩邊不同就是紅燈，不放寬：先查哪一邊才對。

不能比的呼叫照實跳過並寫明原因：呼叫端另外傳了樹裡沒有的 `rules=`（Go 只讀
樹自己的根載體），以及 `disable_effects=False`（Go 一律帶 disable 判決）。
`tenants=` 子集只比要求的那些租戶（子集判決逐鍵等於全樹判決，那邊的測試釘住）。
"""
from __future__ import annotations

import inspect
import itertools
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

import _lib_key_verdict as kv
import test_key_verdict as _tkv  # noqa: E402  (tests/shared on sys.path)
import test_membership_matrix_verdict as _tmm  # noqa: E402

pytestmark = pytest.mark.usefixtures("da_guard_env")

_SOURCES = (_tkv, _tmm)


def _param_sets(fn: Any) -> list[dict[str, Any]] | None:
    """Every argument set pytest would run `fn` with, from its parametrize
    marks (stacked marks multiply); None when it takes a fixture other than
    `tmp_path` (not replayable here)."""
    names = [p for p in inspect.signature(fn).parameters if p != "self"]
    axes: list[list[dict[str, Any]]] = []
    given: set[str] = set()
    for mark in getattr(fn, "pytestmark", []):
        if mark.name != "parametrize":
            continue
        argnames = mark.args[0]
        argnames = [a.strip() for a in argnames.split(",")] if isinstance(argnames, str) else list(argnames)
        rows = []
        for v in mark.args[1]:
            v = v.values if hasattr(v, "values") and hasattr(v, "marks") else v  # pytest.param
            vals = (v,) if len(argnames) == 1 else tuple(v)
            rows.append(dict(zip(argnames, vals)))
        axes.append(rows)
        given |= set(argnames)
    if set(names) - given - {"tmp_path"}:
        return None
    return [dict(item for d in combo for item in d.items()) for combo in itertools.product(*axes)] or [{}]


def _cases() -> list[Any]:
    out = []
    for mod in _SOURCES:
        holders = [(None, mod)] + [(name, obj) for name, obj in vars(mod).items()
                                   if inspect.isclass(obj) and name.startswith("Test")]
        for cls, holder in holders:
            for name, fn in vars(holder).items():
                if not (name.startswith("test_") and inspect.isfunction(fn)):
                    continue
                sets = _param_sets(fn)
                qual = f"{mod.__name__}::{cls + '::' if cls else ''}{name}"
                if sets is None:
                    out.append(pytest.param(holder, cls, fn, None, id=qual))
                    continue
                for i, params in enumerate(sets):
                    out.append(pytest.param(holder, cls, fn, params,
                                            id=qual + (f"[{i}]" if len(sets) > 1 else "")))
    return out


_CASES = _cases()


def _python_errors(verdict: Any) -> set[tuple[str, str, str, str]]:
    return {(t, v.key, v.reason or "", v.file or "")
            for t, rows in verdict.keys.items() for v in rows
            if v.verdict == kv.UNREAD and v.severity == kv.ERROR}


def _go_errors(conf_d: str, at: str | None) -> set[tuple[str, str, str, str]]:
    argv = [os.environ["DA_GUARD_BINARY"], "key-verdict", "--config-dir", str(conf_d)]
    if at is not None:
        argv += ["--at", at]
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", check=False, timeout=120)
    assert proc.returncode == 0, f"da-guard key-verdict rc={proc.returncode}: {proc.stderr}"
    doc = json.loads(proc.stdout)
    assert doc["schema"] == "da-guard.key-verdict/v1"
    return {(e["tenant"], e["key"], e["reason"], e["file"]) for e in doc["errors"]}


def test_the_cases_are_not_truncated():
    """空的案例表永遠綠：本 repo 一再抓到的形狀。"""
    assert len(_CASES) >= 40, len(_CASES)


@pytest.mark.parametrize("holder, cls, fn, params", _CASES)
def test_go_error_layer_equals_python(monkeypatch, tmp_path, holder, cls, fn, params):
    if params is None:
        pytest.skip("takes a fixture other than tmp_path: not replayable here")
    compared: list[str] = []
    skipped: list[str] = []
    real = kv.judge_tree

    def recording(conf_d, **kwargs):
        verdict = real(conf_d, **kwargs)
        if kwargs.get("rules"):
            skipped.append("caller rules= the tree does not carry: Go reads the root carrier only")
            return verdict
        if kwargs.get("disable_effects") is False:
            skipped.append("disable_effects=False: Go always judges with disable effects")
            return verdict
        py = _python_errors(verdict)
        go = _go_errors(conf_d, kwargs.get("at"))
        if kwargs.get("tenants") is not None:
            go = {e for e in go if e[0] in set(kwargs["tenants"])}
        assert go == py, (f"{conf_d}: only Go {sorted(go - py)}; only Python {sorted(py - go)}")
        compared.append(str(conf_d))
        return verdict

    monkeypatch.setattr(kv, "judge_tree", recording)
    args = dict(params)
    if "tmp_path" in inspect.signature(fn).parameters:
        args["tmp_path"] = tmp_path
    if cls is not None:
        fn(holder(), **args)
    else:
        fn(**args)
    if not compared:
        pytest.skip("; ".join(sorted(set(skipped)))
                    or "no judge_tree call returned: a synthetic judge() input, or one the test expects to raise")
