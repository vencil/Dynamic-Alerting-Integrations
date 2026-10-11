"""#2873 — the recipe-preview image carries every module its eval core imports.

The image does not ship ``scripts/tools``: its Dockerfile COPYs a hand-picked
list into ``/opt/recipe-preview/core``. When ``compile_custom_alerts`` started
importing ``_lib_io`` (and ``custom_alerts.loader`` ``_lib_confd`` …), nobody
added them to that list, and ``app.py`` failed at import — so the container
never came up — from 2026-08-28 on. CI only runs ``docker build``, which a
missing *Python* module does not fail.

No docker needed: this lays out exactly what the Dockerfile COPYs, in the
places it COPYs them, and imports ``app.py`` there the way the container
does. A new import without a matching COPY turns this red at PR time.

It also pins the other half of the drift: every ``scripts/`` COPY source must
be in the ``component-docker-build`` path filter, or a change to it never
rebuilds the image in CI.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ops"))
from paths_filter_glob import covers  # noqa: E402  (GitHub's glob, not fnmatch's)
DOCKERFILE = REPO_ROOT / "components" / "recipe-preview" / "Dockerfile"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "component-docker-build.yaml"


def _final_stage(text: str) -> list[str]:
    """Logical lines of the last ``FROM`` stage — the image that ships.

    A trailing backslash continues an instruction on the next line, as in
    Docker; a comment line inside a continued instruction is skipped.
    """
    lines, buf = [], ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if buf and stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        lines.append(buf + stripped)
        buf = ""
    if buf:
        lines.append(buf)
    last = max(i for i, l in enumerate(lines) if l.startswith("FROM "))
    return lines[last:]


def _layout() -> tuple[str, list[tuple[str, str]], dict[str, str]]:
    """(WORKDIR, [(repo src, image dst)], ENV) of the shipped stage.

    Fails on any form it does not model (ADD, COPY flags other than
    ``--from``, several sources, lowercase instructions, several ENV keys) instead of dropping it: a dropped COPY would vanish
    from both checks below at once.
    """
    workdir, copies, env = "/", [], {}
    for line in _final_stage(DOCKERFILE.read_text(encoding="utf-8")):
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            continue
        word = parts[0]
        assert word.upper() not in ("ADD",) and not (
            word.lower() in ("copy", "add", "env", "workdir") and word != word.upper()
        ), f"instruction not modelled: {line!r}"
        if word == "WORKDIR":
            workdir = parts[1]
        elif word == "ENV":
            assert len(parts) == 2 and "=" in parts[1], f"ENV form not modelled: {line!r}"
            key, _, value = parts[1].partition("=")
            env[key] = value
        elif word == "COPY":
            if parts[1].startswith("--from"):
                continue        # a builder artefact, not a repo file
            assert not parts[1].startswith("--"), f"COPY flag not modelled: {line!r}"
            assert len(parts) == 3, f"multi-source COPY not modelled: {line!r}"
            src, dst = parts[1], parts[2]
            dst = dst if dst.startswith("/") else f"{workdir.rstrip('/')}/{dst.removeprefix('./')}"
            copies.append((src, dst))
    return workdir, copies, env


def test_the_layout_was_read():
    """Must-fire for the parser: an empty COPY list would make the import
    test below fail for the wrong reason, or a parser bug pass for one."""
    _, copies, env = _layout()
    srcs = {s for s, _ in copies}
    assert "scripts/tools/dx/compile_custom_alerts.py" in srcs, srcs
    assert "components/recipe-preview/app.py" in srcs, srcs
    assert "PREVIEW_CORE_DIR" in env, env


def test_app_imports_in_the_image_layout(tmp_path):
    workdir, copies, env = _layout()
    root = tmp_path / "img"

    def here(image_path: str) -> Path:
        return root / image_path.lstrip("/")

    for src, dst in copies:
        source, target = REPO_ROOT / src, here(dst)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target,
                            ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, target)
    run_env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                   PREVIEW_CORE_DIR=str(here(env["PREVIEW_CORE_DIR"])))
    # Import app.py as the container does, then compile one recipe with the
    # bundled compiler: `preview_recipe` wraps build_pack in `except Exception`
    # and would turn a module missing at REQUEST time into a 200 `error`
    # answer, which an import-only check cannot see (#2873 review).
    confd = tmp_path / "conf.d"
    confd.mkdir()
    (confd / "a.yaml").write_text(
        "tenants:\n  ta:\n    _custom_alerts:\n"
        '      - {recipe: threshold, name: cpu_hot, metric: node_cpu, op: ">",'
        ' window: 5m, threshold: "80:warning"}\n', encoding="utf-8")
    probe = ("import importlib.util as u, sys, pathlib; "
             "s = u.spec_from_file_location('app', 'app.py'); "
             "m = u.module_from_spec(s); s.loader.exec_module(m); "
             "pack = sys.modules['compile_custom_alerts'].build_pack("
             "pathlib.Path(sys.argv[1])); "
             "assert pack['groups'], pack; print('ok')")
    r = subprocess.run([sys.executable, "-I", "-c", probe, str(confd)],
                       cwd=str(here(workdir)), capture_output=True,
                       timeout=120, env=run_env)
    err = r.stderr.decode("utf-8", "replace")
    assert r.returncode == 0, f"app.py does not import in the image layout:\n{err[-1500:]}"


def _path_filter() -> list[str]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    on = doc.get("on", doc.get(True))   # PyYAML reads a bare `on:` as True
    return on["pull_request"]["paths"]


@pytest.mark.parametrize("src", sorted(
    s for s, _ in _layout()[1] if s.startswith("scripts/")))
def test_every_scripts_copy_source_rebuilds_the_image(src):
    patterns = _path_filter()
    hit = any(covers(p, src) for p in patterns)
    assert hit, f"{src} is COPYed into recipe-preview but not in {WORKFLOW.name}'s paths"
