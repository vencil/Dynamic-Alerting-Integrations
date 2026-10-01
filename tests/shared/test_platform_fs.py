"""The capability probes in ``tests/_platform_fs.py`` (#2559).

A probe-driven skip is only safe if it cannot spread to the hosts where the
tests carry weight: a probe that answered False on Linux CI would turn every
symlink test into a skip under a green run. The first test is that guard.
"""
from __future__ import annotations

import os

import pytest

import _platform_fs as pf


@pytest.mark.skipif(os.name == "nt", reason="Windows may lack the privilege; that is what the probe is for")
def test_a_posix_host_can_create_symlinks():
    """⛔ On POSIX the probe must be True — otherwise the symlink tests are
    skipping where they are supposed to run (CI, the dev container)."""
    assert pf.can_symlink(), (
        "tests/_platform_fs.can_symlink() is False on a POSIX host: every test "
        "using symlink_or_skip / require_symlinks is being skipped here")


def test_without_the_capability_it_skips_and_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "can_symlink", lambda: False)
    link = tmp_path / "link"
    with pytest.raises(pytest.skip.Exception) as ei:
        pf.symlink_or_skip(tmp_path / "target", link)
    assert pf.SYMLINK_SKIP_REASON in str(ei.value)
    assert not os.path.lexists(link)


def test_only_the_missing_capability_skips(tmp_path, monkeypatch):
    """A failure of the call itself is a real failure on every host: it must
    raise, not be absorbed into the skip."""
    monkeypatch.setattr(pf, "can_symlink", lambda: True)

    def refuse(src, dst, **kwargs):
        raise FileExistsError(17, "File exists", str(dst))

    monkeypatch.setattr(pf.os, "symlink", refuse)
    with pytest.raises(FileExistsError):
        pf.symlink_or_skip(tmp_path / "target", tmp_path / "link")


def test_with_the_capability_it_creates_the_link(tmp_path):
    pf.require_symlinks()
    target = tmp_path / "target"
    target.write_text("x\n", encoding="utf-8")
    link = tmp_path / "link"
    pf.symlink_or_skip(target, link)
    assert link.is_symlink()
    assert link.read_text(encoding="utf-8") == "x\n"
