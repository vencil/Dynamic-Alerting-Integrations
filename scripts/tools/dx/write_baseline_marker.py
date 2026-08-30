#!/usr/bin/env python3
"""Write the completeness marker that `bench-baseline.txt` cannot carry itself.

TRK-371 / #1635. `bench-baseline.txt` is a plain `go test -bench` dump: no row
count, no trailer, no checksum. Truncated and genuinely-short are ISOMORPHIC in
that evidence, and `bench-record.yaml` uploads with `if: always()`, so a run
that died mid-benchmark ships whatever bytes exist. That is why
`analyze_bench_history.py` had to key its window on the RUN's conclusion
(`--status success`) — and why a failing CONSUMER job deleted that night from
its own future windows.

This tool writes a sidecar, `bench-baseline.rows`, as the LAST action of the
baseline step. Its ABSENCE is the primary signal: the step did not finish, so
there is no claim that the file is whole. Its CONTENT is the secondary one: a
row count the consumer re-derives and compares.

⛔ THE ROW DEFINITION IS SHARED, NOT DESCRIBED. `bench-record.yaml` already
logged `grep -c '^Benchmark'`, but that counts every line STARTING with
"Benchmark" while the consumer's `_BENCH_RE` additionally requires the `-<cpus>`
suffix, the iteration count and an `ns/op` field. Those two numbers disagree on
exactly the input the marker exists to catch — a line truncated after its name.
So this module imports the consumer's compiled regex rather than restating it;
producer and consumer count identically BY CONSTRUCTION, and the only way the
numbers can differ is that the bytes changed between write and read.

⚠️ WHAT THE MARKER DOES AND DOES NOT ESTABLISH. It says "the step that produced
this file ran to completion, and at that moment the file held N rows". It says
NOTHING about whether N is a scientifically adequate sample — there is still no
sample floor in `night_records_from_gh` (#1635 §5, and that half is explicitly
NOT closed by this tool). Claiming otherwise would be the over-claim this line
has had to correct twice already.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ⛔ Import, do not restate. See the module docstring: a copied regex is the
# one way this design can silently start counting a different thing.
from analyze_bench_history import _BENCH_RE

MARKER_FILE = "bench-baseline.rows"

# ⛔ §P5 (dev-rules): this string and the KEY SET rendered by `render_marker`
# move together. Adding a required key without bumping this is the defect
# `60f4523` shipped on the paired side; `tests/dx/test_write_baseline_marker.py`
# pins the pair by running this producer and reading its real output.
MARKER_SCHEMA = "bench-baseline-rows/v1"


def count_rows(text: str) -> int:
    """Count benchmark observation rows the way the CONSUMER counts them."""
    return sum(1 for line in text.splitlines() if _BENCH_RE.match(line))


def render_marker(rows: int) -> str:
    """Render the marker body. Key order is stable so the file diffs cleanly."""
    return f"schema: {MARKER_SCHEMA}\nrows: {rows}\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--baseline", required=True, type=Path,
                    help="Path to bench-baseline.txt (the file being vouched for).")
    ap.add_argument("--out", required=True, type=Path,
                    help=f"Path to write the marker (conventionally {MARKER_FILE}).")
    args = ap.parse_args(argv)

    if not args.baseline.is_file():
        # ⛔ Never write a marker for a file that is not there. The marker's
        # whole meaning is "I looked, and it was whole"; writing one here would
        # vouch for nothing and the consumer would trust it.
        print(f"::error::{args.baseline} does not exist — refusing to write a "
              "completeness marker", file=sys.stderr)
        return 1

    try:
        text = args.baseline.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"::error::cannot read {args.baseline} ({exc}) — refusing to "
              "write a completeness marker", file=sys.stderr)
        return 1

    rows = count_rows(text)
    if rows == 0:
        # A baseline with zero parseable rows is not a short night, it is a
        # broken one. `night_records_from_gh` already drops it (`if not
        # by_bench`), so vouching for it would only add a lie to the artifact.
        print(f"::error::{args.baseline} has no parseable benchmark rows — "
              "refusing to write a completeness marker", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n": the marker is compared byte-for-byte against what the
    # consumer re-derives, and `.gitattributes` pins `* text=auto eol=lf`. An
    # unpinned write emits CRLF on a Windows host and LF in CI.
    args.out.write_text(render_marker(rows), encoding="utf-8", newline="\n")
    print(f"baseline rows: {rows} (marker: {args.out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
