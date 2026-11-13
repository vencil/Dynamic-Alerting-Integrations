"""One mode for every conf.d file, in Go and in Python (#3535).

A conf.d file is a repo artifact (owner ruling 2026-11-03): committed, read by
another uid. Go writes it in ``config.ConfDFileMode``
(components/threshold-exporter/app/pkg/config/confd_mode.go — da-guard and
tenant-api); Python's repo-artifact writer in
``_lib_atomic_write._DEFAULT_MODE``. This pins the two to one value, read
from the Go source (no Go toolchain needed here).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GO_CONST = REPO_ROOT / "components/threshold-exporter/app/pkg/config/confd_mode.go"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
import _lib_atomic_write  # noqa: E402

_DECL = re.compile(r"^const ConfDFileMode os\.FileMode = (0o[0-7]+)$", re.MULTILINE)


def _go_mode(text: str) -> int:
    found = _DECL.findall(text)
    assert len(found) == 1, f"one `const ConfDFileMode os.FileMode = 0o…` in {GO_CONST}: {found}"
    return int(found[0], 8)


def test_go_and_python_write_conf_d_in_one_mode():
    assert _go_mode(GO_CONST.read_text(encoding="utf-8")) == _lib_atomic_write._DEFAULT_MODE == 0o644


def test_the_reader_refuses_a_declaration_it_cannot_read():
    """The gate's own control: a reworded declaration is not read as a match."""
    import pytest
    with pytest.raises(AssertionError):
        _go_mode("const ConfDFileMode = os.FileMode(0644)\n")
