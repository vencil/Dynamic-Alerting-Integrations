"""Shared VictoriaMetrics harness — vmsingle HTTP client + ``vmalert -replay`` +
promtool-notation utilities (extracted verbatim from the #968 replay/parity tests).

Consumers
---------
* ``test_vm_replay_staleness.py`` — staleness characterization bench (#947/#968).
* ``test_vm_backend_parity.py``   — engine-equivalence anchor (ADR-025 Part 1).
* ``test_vm_alert_parity.py``     — per-PR gate A (binary discovery only).
* the fault-injection CLI (scripts/tools/dx/, ADR-030 PR-2) — loaded via
  ``importlib`` file-path load in a NON-pytest environment.

⛔ Contract: this module must NOT import pytest (the CLI imports it outside any
test run). All skip / ``*_REQUIRE=1`` / ``pytest.fail`` policy lives in the test
files. Failure signalling here uses ``assert`` (verbatim from the original
helpers, so test behavior is unchanged); CLI callers must not run under ``-O``.

Determinism conventions (shared by all consumers):
* ``T0`` fixed epoch — synthetic histories are written at 2023 timestamps,
  never ``now``; the vmsingle MUST run ``-retentionPeriod=100y``.
* ``run_id`` label isolation — ``new_run_id()`` per consumer-module run; each
  run writes physically-new series, so no deletes / tombstone races.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

DEFAULT_VM_URL = "http://localhost:8428"

T0 = 1_700_000_000          # fixed epoch base — deterministic, never `now`
STEP = 30                   # scrape + replay eval interval (s) — the pinned benchmark
                            # interval (#968 replay bench pins are INTERVAL-COUPLED at 30s)

# window_start() slot layout (defaults == the parity anchor's original constants)
GAP = 3_600                 # ingest-window gap (s); >> VM default staleness (5m)
MAX_BLOCKS = 50             # max test-blocks per case (slot budget)
WORKER_SPAN = 1_000         # slots reserved per xdist worker (>> groups*MAX_BLOCKS)


def new_run_id() -> str:
    """Unique per-run isolation tag → each run writes physically-new series (no delete,
    no tombstone race — Gemini #968). uuid is fine here: assertions pin OFFSETS, not the
    tag. Call ONCE at module level in a consumer to keep per-module uniqueness."""
    return "r" + uuid.uuid4().hex[:12]


# ---- binary discovery ------------------------------------------------------
def find_vmalert() -> str | None:
    """Locate the ``vmalert`` binary: $VMALERT → PATH → dev-container /tmp/vm."""
    env = os.environ.get("VMALERT")
    if env and Path(env).exists():
        return env
    for name in ("vmalert", "vmalert-prod"):
        found = shutil.which(name)
        if found:
            return found
    fallback = Path("/tmp/vm/vmalert-prod")   # dev-container provisioning
    return str(fallback) if fallback.exists() else None


def find_vmalert_tool() -> str | None:
    """Locate ``vmalert-tool``: $VMALERT_TOOL → PATH → dev-container /tmp/vm."""
    env = os.environ.get("VMALERT_TOOL")
    if env and Path(env).exists():
        return env
    for name in ("vmalert-tool", "vmalert-tool-prod"):
        found = shutil.which(name)
        if found:
            return found
    fallback = Path("/tmp/vm/vmalert-tool-prod")  # jitter-harness dev-container download
    return str(fallback) if fallback.exists() else None


# ---- vmsingle HTTP client ---------------------------------------------------
class VMClient:
    """Thin HTTP client for one vmsingle (import / flush / query)."""

    def __init__(self, base_url: str = DEFAULT_VM_URL) -> None:
        self.base_url = base_url

    def reachable(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    def import_prometheus(self, lines: list[str]) -> None:
        """POST Prometheus-exposition lines to /api/v1/import/prometheus."""
        if not lines:
            return
        body = ("\n".join(lines) + "\n").encode()
        req = urllib.request.Request(
            f"{self.base_url}/api/v1/import/prometheus", data=body, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status in (200, 204), f"VM import failed: {r.status}"

    def import_series(self, series_label_str: str, values: list[float | None],
                      t0_ms: int, interval_ms: int) -> None:
        """Import one series to VM (line per sample, absolute ms timestamps)."""
        self.import_prometheus([f"{series_label_str} {v} {t0_ms + i * interval_ms}"
                                for i, v in enumerate(values) if v is not None])

    def flush(self) -> None:
        """Force VM to flush in-memory buffers so just-imported data is queryable.
        Single-node-only endpoint; raises on failure — callers that may tolerate a
        missing endpoint (cluster VM) wrap this with their own policy."""
        with urllib.request.urlopen(f"{self.base_url}/internal/force_flush", timeout=10) as r:
            r.read()

    def query_range(self, expr: str, start: int, end: int, step: int) -> list[dict]:
        qs = urllib.parse.urlencode({"query": expr, "start": str(start), "end": str(end),
                                     "step": f"{step}s", "nocache": "1"})
        with urllib.request.urlopen(f"{self.base_url}/api/v1/query_range?{qs}", timeout=20) as r:
            d = json.loads(r.read())
        assert d.get("status") == "success", f"VM query error: {d}"
        return d["data"]["result"]

    def query_instant(self, expr: str, at_s: int) -> list[dict]:
        """Instant query VM at absolute time `at_s`; nocache=1 (Gemini trap #2)."""
        qs = urllib.parse.urlencode({"query": expr, "time": str(at_s), "nocache": "1"})
        req = urllib.request.Request(f"{self.base_url}/api/v1/query?{qs}")
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        assert data.get("status") == "success", f"VM query error: {data}"
        return data["data"]["result"]


def alert_offsets(client: VMClient, name: str, state: str, w_start: int, w_end: int,
                  *, run_id: str, step: int = STEP) -> list[int]:
    """Offsets (s, relative to `w_start`) at which ``ALERTS{alertname,alertstate}`` has
    samples. run_id-scoped: ALERTS inherit run_id from the alert expressions' output
    labels, so this read only sees THIS run's alerts even on a shared, never-deleted
    vmsingle."""
    res = client.query_range(
        f'ALERTS{{alertname="{name}",alertstate="{state}",run_id="{run_id}"}}',
        w_start, w_end, step)
    return sorted(int(v[0]) - w_start for s in res for v in s["values"])


# ---- vmalert -replay --------------------------------------------------------
def replay(vmalert_bin: str, rules_text: str, w_start: int, w_end: int, tmp: Path,
           tag: str, *, datasource_url: str, remote_write_url: str | None = None,
           timeout_s: int = 120, rules_delay_s: int | None = None,
           remote_write_flush_interval_s: int | None = None) -> None:
    """Run ``vmalert -replay`` for `rules_text` over [w_start, w_end] (epoch seconds).
    Writes the rules to `tmp`, replays against `datasource_url`, remote-writes ALERTS
    back to `remote_write_url` (defaults to `datasource_url`). Does NOT flush — call
    ``client.flush()`` before querying the replay-written ALERTS.

    `rules_delay_s` / `remote_write_flush_interval_s`: optional record→alert chain
    visibility knobs (``-replay.rulesDelay`` / ``-remoteWrite.flushInterval``; vmalert
    docs require rulesDelay >= flushInterval). ``None`` (default) OMITS the flag —
    existing #968 callers keep the exact original command line (parity guard).

    Engine-failure contract（實測 pin，vmalert v1.146.0）：rule 的 query 在 eval 期
    出錯（如 422）→ 內建 retry（``-replay.ruleRetryAttempts`` 預設 5 次）→ Fatalf
    終止整個 process——exit 255、不印 "replay succeed!"、不會續跑其他規則（以
    ``-search.maxSamplesPerQuery=1`` 強迫 eval error 親驗）。下方雙因子 assert
    （rc==0 AND "replay succeed"）因此是「引擎跑不動」不會偽裝成「規則沒 fire」
    的 fail-loud 防線；VM pin 升版時重驗此契約。"""
    rf = tmp / f"{tag}_rules.yml"
    rf.write_text(rules_text, encoding="utf-8")
    tf = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(w_start))
    tt = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(w_end))
    rw = remote_write_url if remote_write_url is not None else datasource_url
    cmd = [vmalert_bin, f"-rule={rf.as_posix()}", f"-datasource.url={datasource_url}",
           f"-remoteWrite.url={rw}",
           f"-replay.timeFrom={tf}", f"-replay.timeTo={tt}", "-replay.disableProgressBar"]
    if remote_write_flush_interval_s is not None:
        cmd.append(f"-remoteWrite.flushInterval={int(remote_write_flush_interval_s)}s")
    if rules_delay_s is not None:
        cmd.append(f"-replay.rulesDelay={int(rules_delay_s)}s")
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    assert p.returncode == 0 and "replay succeed" in p.stderr, (
        f"vmalert -replay failed for {tag}:\n{p.stderr[-1500:]}")


# ---- promtool series-values notation ----------------------------------------
def expand_values(spec: str) -> list[float | None]:
    """Expand promtool `values:` notation → list of samples (None == gap `_`).

    Grammar: space-separated tokens, each: `v` | `_` gap | `vxN` | `v+dxN` | `v-dxN`.
    `v+dxN` = v, v+d, ..., v+N*d  (N+1 samples). `vxN` = v repeated N+1 times.
    Scientific notation (`1e-3`) is supported as a plain value.
    """
    out: list[float | None] = []
    for tok in spec.split():
        if tok == "_":
            out.append(None)
            continue
        if "x" in tok:
            base, count_s = tok.rsplit("x", 1)
            count = int(count_s)
            delta = 0.0
            if "+" in base:                       # `a+d` incrementing
                a_s, d_s = base.split("+", 1)
                a, delta = float(a_s), float(d_s)
            else:
                try:
                    a = float(base)               # plain repeat (incl `1e-3`, `-5`)
                except ValueError:                # `a-d` decrementing (a may be negative)
                    lead = "-" if base.startswith("-") else ""
                    a_s, d_s = base[len(lead):].split("-", 1)
                    a, delta = float(lead + a_s), -float(d_s)
            for i in range(count + 1):
                out.append(a + i * delta)
        else:
            out.append(float(tok))
    return out


def parse_dur(d) -> int:
    """Minimal promtool duration → seconds (`15s`,`5m`,`1h`,`2h30m`...)."""
    if isinstance(d, (int, float)):
        return int(d)
    total, num = 0, ""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    for ch in str(d):
        if ch.isdigit():
            num += ch
        elif ch in units:
            total += int(num or 0) * units[ch]
            num = ""
    return total or int(num or 0)


# ---- deterministic ingest-window slotting ------------------------------------
def worker_offset(span: int = WORKER_SPAN) -> int:
    """Per-xdist-worker slot offset so parallel workers sharing one VM can't collide.
    pytest-xdist-specific: reads $PYTEST_XDIST_WORKER (``gw<N>``); outside xdist (or in
    a CLI) the env var is absent → offset 0."""
    w = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    return int("".join(c for c in w if c.isdigit()) or "0") * span


def window_start(group_id: int, block_idx: int, *, t0: int = T0, gap_s: int = GAP,
                 max_blocks: int = MAX_BLOCKS, worker_span: int = WORKER_SPAN) -> int:
    """Unique, DETERMINISTIC ingest-window start for one (worker, case, test-block).
    group_id is a global per-case index; each window is `gap_s` apart (>> VM staleness)
    so no two logical tests cross-talk, and re-runs re-import identically (idempotent)."""
    slot = group_id * max_blocks + block_idx
    assert slot < worker_span, f"slot {slot} overflows worker span (raise worker_span)"
    return t0 + (worker_offset(worker_span) + slot) * gap_s
