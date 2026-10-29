"""A stand-in for the `subprocess.run` that `_lib_kubectl` runs kubectl with.

`_lib_kubectl._answer` hands kubectl files for stdout and stderr and reads
them once kubectl has exited (#3349 review: a pipe a leftover child still
holds would keep an answer open until that child exits). A fake that only
returned a `CompletedProcess` would leave those files empty; `kubectl_run`
writes what the fake answers where the runner reads it.
"""
from __future__ import annotations


def kubectl_run(fake):
    """`subprocess.run` for `_lib_kubectl` from *fake* — a callable taking the
    run's arguments and returning an object with `returncode`, `stdout` and
    `stderr` (a `CompletedProcess`, a MagicMock …)."""
    def run(cmd, *args, **kwargs):
        answer = fake(cmd, *args, **kwargs)
        for stream in ("stdout", "stderr"):
            sink = kwargs.get(stream)
            text = getattr(answer, stream, None) or ""
            if hasattr(sink, "write") and isinstance(text, (str, bytes)):
                sink.write(text.encode("utf-8") if isinstance(text, str) else text)
        return answer
    return run
