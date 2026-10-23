"""An upstream that answers, for tests that measure something else.

A tool that queries Prometheus fails closed when no answer comes (#3133:
not measured is rc 2), so a test about how the tool reads its conf.d tree
must give it a Prometheus that answers — empty, well-formed answers from
``test_json_stdout_contract._StubHandler``, the one stub of the suite.
"""
from __future__ import annotations

import functools
import threading
from http.server import ThreadingHTTPServer

import test_json_stdout_contract as _jsc  # noqa: E402  (tests/shared on sys.path)


@functools.lru_cache(maxsize=None)
def url() -> str:
    """Base URL of the stub, started once per process (daemon threads)."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _jsc._StubHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}"
