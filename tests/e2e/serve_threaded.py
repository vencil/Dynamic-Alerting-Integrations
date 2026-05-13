#!/usr/bin/env python3
"""Threaded HTTP server for portal E2E tests (TRK-232e P1 perf).

Why this exists
---------------
`python -m http.server` uses `HTTPServer` (single-threaded). Each request
serializes against the previous one, which is fine when Playwright runs
with --workers=1, but blocks effective parallelization: with workers=2
we observed ~60 / 192 spec failures because two browser contexts racing
to fetch JSX dist files would back up requests until per-test timeouts
fired.

Switching to `ThreadingHTTPServer` (also in stdlib http.server) lets the
server handle each request in its own thread. The static file fetches
are the only work being done — there's no shared mutable state to
worry about — so threading is safe and cheap.

Usage:
    python serve_threaded.py [port] [directory]
    # defaults: port 8080, directory ../../docs

Started by `npm run serve:portal` (see tests/e2e/package.json).

Exit
----
SIGINT / SIGTERM stops cleanly via the `with` block. CI's "Start Portal
server" step also kills via process group on workflow teardown.
"""
from __future__ import annotations

import os
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
DIRECTORY = sys.argv[2] if len(sys.argv) > 2 else os.path.join('..', '..', 'docs')

handler = partial(SimpleHTTPRequestHandler, directory=DIRECTORY)

with ThreadingHTTPServer(('', PORT), handler) as srv:
    abs_dir = os.path.abspath(DIRECTORY)
    print(f'[portal] ThreadingHTTPServer on :{PORT} (dir={abs_dir})', flush=True)
    srv.serve_forever()
