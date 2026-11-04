"""pytest plugin for test_key_verdict_go_parity.py (#3489): wraps
`_lib_key_verdict.judge_tree` so that every call that returns — from the
verdict's own tests or through validate-config, explain-keys, the route
generator, the generator container's child — is compared with `da-guard
key-verdict` on the same tree, at the same instant, for the same tenants.

A call Go cannot reproduce as made is compared as the tree itself reads:
`rules=` (rules the tree does not carry: Go reads the root carrier only) and
`disable_effects=False` (Go always judges with disable effects) are judged
again by Python without them, and that verdict is compared.

Each comparison is one JSON line appended to $KEY_VERDICT_PARITY_LOG
({"conf_d", "ok", "diff"}): a mismatch must not be swallowed by a caller
that turns an exception into "not judged" (the generator container's child
does), so the parent test reads the log instead of an assertion here.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any

import pytest


def python_doc(verdict: Any) -> dict[str, Any]:
    """The Python verdict in key-verdict/v2's shape, JSON-normalised."""
    doc = {
        "undecidable": verdict.undecidable,
        "tenants": {t: [{"key": v.key, "layer": v.layer, "file": v.file, "read_by": list(v.read_by),
                         "verdict": v.verdict, "severity": v.severity, "reason": v.reason,
                         "disable": v.disable, "served_family": list(v.served_family),
                         "value": v.value, "served": v.served} for v in rows]
                    for t, rows in verdict.keys.items()},
        "findings": [dict(f._asdict(), tenants=list(f.tenants), served_family=list(f.served_family),
                          served_from=list(f.served_from)) for f in verdict.findings],
        "key_checks": {t: {"version_labels": list(kc.version_labels), "renames": list(kc.renames)}
                       for t, kc in verdict.key_checks.items() if kc is not None},
    }
    return json.loads(json.dumps(doc))


def go_doc(conf_d: str, at: str | None, tenants: Any) -> dict[str, Any]:
    argv = [os.environ["DA_GUARD_BINARY"], "key-verdict", "--config-dir", str(conf_d)]
    if at is not None:
        argv += ["--at", at]
    for t in ([tenants] if isinstance(tenants, str) else tenants) or ():
        argv += ["--tenant", t]
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", check=False, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"da-guard key-verdict rc={proc.returncode}: {proc.stderr}")
    doc = json.loads(proc.stdout)
    if doc.pop("schema") != "da-guard.key-verdict/v2":
        raise RuntimeError("not a key-verdict/v2 document")
    for k in ("at", "tenants_requested", "tenants_absent"):
        doc.pop(k, None)
    return doc


def diff(go: dict[str, Any], py: dict[str, Any]) -> str:
    """The first differences, readable."""
    out = []
    for part in ("undecidable", "key_checks"):
        if go.get(part) != py.get(part):
            out.append(f"{part}: Go {go.get(part)!r} / Python {py.get(part)!r}")
    for t in sorted(set(go["tenants"]) | set(py["tenants"])):
        g = {e["key"]: e for e in go["tenants"].get(t, [])}
        p = {e["key"]: e for e in py["tenants"].get(t, [])}
        for k in sorted(set(g) | set(p)):
            if g.get(k) != p.get(k):
                out.append(f"{t} {k}: Go {g.get(k)!r} / Python {p.get(k)!r}")
        if [e["key"] for e in go["tenants"].get(t, [])] != [e["key"] for e in py["tenants"].get(t, [])]:
            out.append(f"{t}: key order differs")
    if go["findings"] != py["findings"]:
        out.append(f"findings: Go {go['findings']!r} / Python {py['findings']!r}")
    return "\n".join(out[:12])


def _log(entry: dict[str, Any]) -> None:
    with open(os.environ["KEY_VERDICT_PARITY_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def pytest_configure(config: pytest.Config) -> None:  # noqa: ARG001
    import _lib_key_verdict as kv

    real = kv.judge_tree

    def recording(conf_d, **kwargs):
        verdict = real(conf_d, **kwargs)
        try:
            again = {k: v for k, v in kwargs.items()
                     if k not in ("rules", "disable_effects", "served_tree")}
            ours = verdict
            if kwargs.get("rules") or kwargs.get("disable_effects") is False:
                ours = real(conf_d, **again)
            py = python_doc(ours)
            go = go_doc(str(conf_d), kwargs.get("at"), kwargs.get("tenants"))
            _log({"conf_d": str(conf_d), "ok": go == py, "diff": "" if go == py else diff(go, py)})
        except Exception as exc:  # noqa: BLE001 — recorded, the parent test fails on it
            _log({"conf_d": str(conf_d), "ok": False, "diff": f"{type(exc).__name__}: {exc}"})
        return verdict

    kv.judge_tree = recording
