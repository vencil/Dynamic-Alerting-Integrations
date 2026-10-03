"""Go modules in this tree that are vendored upstream source, not this repo's code.

One list, read by every guard that enumerates the tracked go.mod files
(toolchain parity, lint coverage, CI compile inputs), so each of them names
the same exemption for the same reason instead of keeping its own copy.

What a vendored module gets instead of those checks is a byte-for-byte pin
against its upstream release; the entry says which test holds it.
"""
from __future__ import annotations

VENDORED_GO_MODULES: dict[str, str] = {
    "components/threshold-exporter/app/third_party/yaml.v3": (
        "#2681: upstream gopkg.in/yaml.v3 v3.0.1 plus one pinned decode.go hunk, "
        "swapped in by `replace` in the exporter and tenant-api go.mod; "
        "tests/ops/test_vendored_yaml_v3.py pins every byte. No `go` directive, "
        "upstream style and upstream tests: none of it may be edited to suit "
        "this repo's checks"),
}
