"""Stub every check in ``pr_preflight`` for tests that drive its ``main()``.

The set is derived from the module, never listed: a check a list misses is
not stubbed and really runs — network, ``gh``, or a 300s
``pre-commit --all-files`` — while the test stays green (#1953).
"""
from __future__ import annotations

import inspect


def stub_checks(monkeypatch, mod, status_of):
    """Replace every ``check_*`` callable in ``mod``; return the names.

    Each stub returns ``CheckResult(<function name>, status_of(<name>), ...)``
    and binds its arguments against the real signature.
    """
    names = sorted(
        n for n, v in vars(mod).items() if n.startswith("check_") and callable(v)
    )
    # ⛔ Must-fire control: deriving nothing would stub nothing — "could not
    # see the checks" must not look like "there are no checks".
    assert names, f"derived no check_* from {mod.__name__} — this stub measures nothing"
    for name in names:
        sig = inspect.signature(getattr(mod, name))

        def stub(*a, _n=name, _sig=sig, **kw):
            _sig.bind(*a, **kw)
            return mod.CheckResult(_n, status_of(_n), "stubbed")

        monkeypatch.setattr(mod, name, stub)
    return names
