"""The route generator reads Alertmanager configs its consumers' way (ADR-036 §12, #3206).

The ConfigMap it emits is read back the way kubectl reads it (`da-crdecode`)
and the config in it the way Alertmanager loads it (`da-read alertmanager`);
without either binary the run stops at exit 2 and writes nothing — it never
falls back to PyYAML. Without amtool, Alertmanager's loader is the verdict
on the emitted config: a refusal is exit 1, nothing written.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"

pytestmark = pytest.mark.usefixtures("da_guard_env", "da_crdecode_env", "da_read_env")

_DEFAULTS = "defaults:\n  mysql_connections: 80\n"


def _tree(root: Path, url: str = "https://hook.example/x") -> Path:
    d = root / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    (d / "db-a.yaml").write_text(
        "tenants:\n  db-a:\n    mysql_connections: \"70\"\n"
        "    _routing:\n      receiver:\n        type: webhook\n"
        f"        url: {url}\n", encoding="utf-8")
    return d


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    # amtool off PATH: these tests are about the readers, not amtool's verdict
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                           if not (shutil.which("amtool", path=p)))
    return subprocess.run([sys.executable, str(GAR), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env={**os.environ, "PATH": path, **env}, timeout=180)


def test_control_the_configmap_is_written(tmp_path) -> None:
    out = tmp_path / "cm.yaml"
    p = _run(["--config-dir", str(_tree(tmp_path)), "--output-configmap", "-o", str(out)], {})
    assert p.returncode == 0, p.stderr
    assert out.is_file()


@pytest.mark.parametrize("env_name", ["DA_CRDECODE_BINARY", "DA_READ_BINARY"])
def test_no_reader_is_exit_2_and_nothing_written(tmp_path, env_name) -> None:
    out = tmp_path / "cm.yaml"
    p = _run(["--config-dir", str(_tree(tmp_path)), "--output-configmap", "-o", str(out)],
             {env_name: str(tmp_path / "no-such-binary")})
    assert p.returncode == 2, p.stderr
    assert "nothing was written" in p.stderr
    assert not out.exists()


def test_validate_without_da_crdecode_is_exit_2(tmp_path) -> None:
    p = _run(["--config-dir", str(_tree(tmp_path)), "--validate"],
             {"DA_CRDECODE_BINARY": str(tmp_path / "no-such-binary")})
    assert p.returncode == 2, (p.stdout, p.stderr)
    assert "the way kubectl does" in (p.stdout + p.stderr)


def test_without_amtool_alertmanagers_loader_refusal_is_exit_1(tmp_path) -> None:
    """`http://[1]/` passes the generator's own checks and is refused by
    Alertmanager at load (#2219). Without amtool it used to be written at
    exit 0 with a NOTICE; Alertmanager's loader (da-read) now refuses it."""
    out = tmp_path / "cm.yaml"
    p = _run(["--config-dir", str(_tree(tmp_path, url="http://[1]/")),
              "--output-configmap", "-o", str(out)], {})
    assert p.returncode == 1, p.stderr
    assert "Alertmanager's config loader refuses" in p.stderr
    assert not out.exists()
