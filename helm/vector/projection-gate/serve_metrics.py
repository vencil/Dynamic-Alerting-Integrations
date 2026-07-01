#!/usr/bin/env python3
"""serve_metrics.py — long-lived exposer for the projection-gate verdict metric
(ADR-021 Phase 2(a) / #908 PR-3).

WHY THIS EXISTS
---------------
`verify_tenant_projections.py` runs as a one-shot INIT-container and writes its
verdict to a Prometheus textfile (`--metrics-file`) in a pod-local emptyDir. But
an init-container exits before Vector starts, so nothing is left listening — the
textfile just sits in the emptyDir, unscrapeable.

The classic way to scrape a boot-time textfile is a node-exporter textfile
collector, but this platform's cluster ships no node-exporter (k8s/03-monitoring
has kube-state-metrics + Prometheus only), and Vector's own prometheus_exporter
reads `internal_metrics`, not an external textfile. So this tiny sidecar serves
the verdict file over HTTP, scraped via a headless Service exactly like the Vector
self-telemetry Service (Prometheus `role: service` SD, job `monitoring-components`).

The exposer is deliberately a SEPARATE, long-lived concern from the validator:
  - It NEVER re-evaluates the gate. The verdict is decided ONCE, at boot, by the
    init-container (the gate's one-shot-at-boot trust contract — see
    verify_tenant_projections.py DEPLOYMENT CONTRACT). This process only re-reads
    and re-serves whatever file the init-container already wrote; it does not read
    the registry, does not import the validator, and holds no trust.
  - It lives in the SAME pod as the init-container's emptyDir, so the metric is
    tied to the pod lifetime: when the pod dies (OOM / node loss) the endpoint
    disappears and the series goes ABSENT rather than going stale "ok" — which is
    exactly what the gate's verdict-metric design wants (a crash must not falsely
    auto-resolve a real mismatch). The alert rules treat the DaemonSet's gate
    verdict in aggregate (every node mounts the identical registry + projections,
    so every node computes the identical verdict); the enforce-mode CrashLoopBackOff
    case — where the init never completes and this sidecar therefore never starts —
    is covered separately by a kube-state-metrics init-container alert.

The file is re-read on every request (not cached) so a re-run init (pod restart →
fresh verdict) is reflected without restarting this process.
"""
from __future__ import annotations

import argparse
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Prometheus text exposition content-type (matches what node-exporter / client
# libraries emit). Prometheus is lenient on the version token but pinning it keeps
# the scrape unambiguous.
_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Served when the verdict file is absent/unreadable. A successful scrape that
# yields NO `vector_tenant_projection_gate_info` series is the correct state for
# "the init-container has not written a verdict": the degrade alert (which keys on
# the metric) stays silent, and a genuine init failure surfaces via the
# kube-state-metrics init-container alert instead — not via a fabricated verdict.
_ABSENT_BODY = "# projection-gate verdict file absent — init-container has not written a verdict yet\n"


def _read_metrics(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return _ABSENT_BODY.encode("utf-8")


def _make_handler(metrics_file: Path) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        # Serve the verdict at ANY path so the scrape works regardless of the
        # Service's prometheus.io/path annotation (we set /metrics, but a path
        # mismatch should still expose the data rather than 404).
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            body = _read_metrics(metrics_file)
            self.send_response(200)
            self.send_header("Content-Type", _CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            # Silence per-request stderr logging — Prometheus scrapes every ~15s
            # and the access log is pure noise in the pod's logs.
            return

    return _Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="HTTP exposer for the projection-gate verdict metric (#908).")
    ap.add_argument("--metrics-file", required=True, type=Path,
                    help="path to the Prometheus textfile the init-container wrote (shared emptyDir)")
    ap.add_argument("--port", type=int, default=9599, help="listen port (default 9599)")
    ap.add_argument("--addr", default="0.0.0.0", help="listen address (default 0.0.0.0)")
    args = ap.parse_args(argv)

    server = ThreadingHTTPServer((args.addr, args.port), _make_handler(args.metrics_file))
    print(f"[projection-gate-exposer] serving {args.metrics_file} on {args.addr}:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
