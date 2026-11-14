"""A stand-in for the kubectl that `_lib_kubectl.kubectl_run` starts.

kubectl is started in one place: `_lib_kubectl.kubectl_run` locates it
(`_lib_engine.locate`) and runs it (`_lib_engine.run`, `capture="file"`),
which hands kubectl files for stdout and stderr and reads them once kubectl
has exited (#3349 review: a pipe a leftover child still holds would keep an
answer open until that child exits). So a fake has two things to stand in
for:

* the lookup — `located` answers `kubectl` whatever PATH holds (a CI runner
  has one, a dev box may not), so the argv a fake sees starts with the bare
  name `kubectl`;
* the run — a fake that only returned a `CompletedProcess` would leave those
  files empty; `kubectl_run` writes what the fake answers where the runner
  reads it.

`install` does both.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "tools"))
import _lib_engine  # noqa: E402


def kubectl_run(fake):
    """`subprocess.run` for `_lib_engine.run` from *fake* — a callable taking
    the run's arguments and returning an object with `returncode`, `stdout`
    and `stderr` (a `CompletedProcess`, a MagicMock …)."""
    def run(cmd, *args, **kwargs):
        answer = fake(cmd, *args, **kwargs)
        for stream in ("stdout", "stderr"):
            sink = kwargs.get(stream)
            text = getattr(answer, stream, None) or ""
            if hasattr(sink, "write") and isinstance(text, (str, bytes)):
                sink.write(text.encode("utf-8") if isinstance(text, str) else text)
        return answer
    return run


def located(monkeypatch):
    """`_lib_engine.locate("kubectl")` finds a kubectl named `kubectl`,
    whatever PATH holds; every other engine is located as usual."""
    real = _lib_engine.locate

    def locate(engine, **kwargs):
        if engine == "kubectl":
            return _lib_engine.Located("kubectl", "binary", path="kubectl")
        return real(engine, **kwargs)
    monkeypatch.setattr(_lib_engine, "locate", locate)


def install(monkeypatch, fake):
    """Every kubectl `_lib_kubectl.kubectl_run` starts is *fake* (see
    `kubectl_run`): its argv starts with the bare name `kubectl`."""
    located(monkeypatch)
    monkeypatch.setattr(_lib_engine.subprocess, "run", kubectl_run(fake))


def run_from_argv(fake):
    """A stand-in for `kubectl_run` itself (the name a tool imported it
    under), for a fake that takes kubectl's whole argv:
    `fake(["kubectl", verb, *args], timeout=…, stdin_text=…)`."""
    def kubectl_run(verb, args, *, timeout, stdin_text=None, **_kwargs):
        return fake(["kubectl", verb, *args], timeout=timeout, stdin_text=stdin_text)
    return kubectl_run
