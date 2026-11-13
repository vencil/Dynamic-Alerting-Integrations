"""lint_jsx_syntax parses the portal's sources with the build's own esbuild (#3669).

The parse is the real one: the lockfile's esbuild under tools/portal/node_modules
and the build's transform settings from tools/portal/esbuild-shared.mjs. So
these tests need that install (`make portal-deps`); without it they skip, and
with ``VIBE_REQUIRE_PORTAL_ESBUILD=1`` (the CI test jobs install it) a missing
install fails instead — "nothing ran" stays apart from "nothing wrong".

The no-verdict cases (no node, no esbuild, a node run that did not cover every
file) need no esbuild and always run.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "lint_jsx_syntax.py"
PORTAL = REPO_ROOT / "tools" / "portal"
_ENV = {k: v for k, v in os.environ.items()
        if k not in ("PYTHONUNBUFFERED", "DA_TEST_CRASH_EXIT_CODE")}


def _esbuild_installed() -> bool:
    return (PORTAL / "node_modules" / "esbuild" / "package.json").is_file()


def _require_esbuild() -> None:
    if _esbuild_installed() and shutil.which("node"):
        return
    msg = "the portal's esbuild is not installed (make portal-deps) or node is missing"
    if os.environ.get("VIBE_REQUIRE_PORTAL_ESBUILD") == "1":
        pytest.fail(f"VIBE_REQUIRE_PORTAL_ESBUILD=1 but {msg} — the CI install step regressed")
    pytest.skip(msg)


def _run(*argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_SCRIPT), *argv], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=180,
                          env=_ENV if env is None else env, stdin=subprocess.DEVNULL)


def _tree(tmp_path: Path, files: dict[str, bytes | str]) -> Path:
    src = tmp_path / "src"
    for name, body in files.items():
        p = src / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, str):
            body = body.encode("utf-8")
        p.write_bytes(body)
    return src


_OK = "export default function A() { return <div>ok</div>; }\n"

#: Broken sources — each must be named and fail --ci. The parity set measured
#: against the Babel lint this replaced (#3669): every case it caught, plus the
#: duplicate `export default` it missed (its import rewrite deleted one).
BROKEN = {
    # NUL in code position (FUSE damage, dev-rules #11); inside a string or
    # JSX text it is a valid character, for esbuild as for the browser.
    "nul_byte.jsx": b"export const a = 1;\x00\nexport default function A() { return null; }\n",
    "unclosed_tag.jsx": "export default function A() { return <div>; }\n",
    "mismatched_tag.jsx": "export default function A() { return <div>x</span>; }\n",
    "import_in_function.jsx": "function f() { import x from 'y'; }\nexport default f;\n",
    "export_in_block.jsx": "if (1) { export const a = 1; }\n",
    "stray_brace.jsx": "export default function A() { return 1; }}\n",
    "unterminated_string.jsx": "export const a = 'abc;\n",
    "unterminated_template.jsx": "export const a = `abc;\n",
    "duplicate_default.jsx": "export default function A() {}\nexport default function B() {}\n",
    # No import/export of its own: parsed as a module all the same, as it ships.
    "top_level_return.js": "const a = 1;\nreturn a;\n",
    "legacy_octal.js": "const a = 010;\n",
    "html_comment.jsx": "export default function A() { return <div><!-- c --></div>; }\n",
    "unclosed_front_matter.jsx": "---\ntitle: x\nexport default function A() { return null; }\n",
    "typescript_in_jsx.jsx": "export const a: number = 1;\n",
}

VALID = {
    "style_double_curly.jsx":
        "export default function A() { return <div style={{ color: 'red' }}>x</div>; }\n",
    "fragment.jsx":
        "import { Copy } from 'lucide-react';\n"
        "export default function A() { return <><Copy /><div /></>; }\n",
    "optional_chaining.js": "export const f = (a) => a?.b ?? 1;\n",
    "front_matter.jsx":
        "---\ntitle: x\npurpose: |\n  ------\n  separators inside the block\n---\n" + _OK,
    "top_level_await.js": "export const a = await Promise.resolve(1);\n",
}


@pytest.mark.parametrize("name", sorted(BROKEN))
def test_a_broken_source_is_named_and_fails_ci(tmp_path, name):
    _require_esbuild()
    src = _tree(tmp_path, {name: BROKEN[name], "fine.jsx": _OK})
    proc = _run("--ci", "--src", str(src))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"{name}:" in proc.stdout, proc.stdout
    assert "fine.jsx" not in proc.stdout.replace("files OK", ""), proc.stdout
    assert "Summary: 1/2 files OK" in proc.stdout, proc.stdout


@pytest.mark.parametrize("name", sorted(BROKEN))
def test_without_ci_a_finding_is_reported_not_fatal(tmp_path, name):
    _require_esbuild()
    proc = _run("--src", str(_tree(tmp_path, {name: BROKEN[name]})))
    assert proc.returncode == 0, proc.stderr
    assert f"{name}:" in proc.stdout


def test_valid_sources_pass(tmp_path):
    _require_esbuild()
    proc = _run("--ci", "--src", str(_tree(tmp_path, VALID)))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"All {len(VALID)} portal source files parse" in proc.stdout


def test_an_error_carries_its_line_in_the_file_itself(tmp_path):
    """The front matter is replaced by blank lines, so the line esbuild names
    is the file's own — line 7 here, under a six-line front matter."""
    _require_esbuild()
    src = _tree(tmp_path, {"a.jsx": "---\na: 1\nb: 2\nc: 3\nd: 4\n---\n"
                                    "export const x = 'unterminated;\n"})
    proc = _run("--ci", "--src", str(src))
    assert proc.returncode == 1
    assert "a.jsx:7:" in proc.stdout, proc.stdout


def test_a_file_that_is_not_utf8_is_a_finding(tmp_path):
    _require_esbuild()
    src = _tree(tmp_path, {"latin1.jsx": "export const a = 'caf\xe9';\n".encode("latin-1"),
                           "fine.jsx": _OK})
    proc = _run("--ci", "--src", str(src))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "latin1.jsx: not UTF-8" in proc.stdout


def test_every_real_portal_source_parses():
    _require_esbuild()
    proc = _run("--ci")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    n = sum(1 for p in (PORTAL / "src").rglob("*") if p.suffix in (".jsx", ".js"))
    assert f"All {n} portal source files parse" in proc.stdout, proc.stdout


# ── no verdict: rc 2, --ci or not, never 0 ───────────────────────────────

@pytest.mark.parametrize("ci", [["--ci"], []], ids=["ci", "report"])
def test_no_esbuild_is_no_verdict_with_the_remedy(tmp_path, ci):
    """A portal dir with esbuild-shared.mjs but no node_modules."""
    if not shutil.which("node"):
        pytest.skip("node is not on PATH")
    portal = tmp_path / "portal"
    portal.mkdir()
    shutil.copy(PORTAL / "esbuild-shared.mjs", portal)
    (portal / "package.json").write_text('{"name": "p", "private": true}', encoding="utf-8")
    src = _tree(tmp_path, {"a.jsx": _OK})
    proc = _run(*ci, "--portal-dir", str(portal), "--src", str(src))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "esbuild is not installed" in proc.stderr and "make portal-deps" in proc.stderr


@pytest.mark.parametrize("ci", [["--ci"], []], ids=["ci", "report"])
def test_no_node_is_no_verdict(tmp_path, ci):
    src = _tree(tmp_path, {"a.jsx": _OK})
    proc = _run(*ci, "--src", str(src), env=dict(_ENV, PATH=str(tmp_path / "empty-bin")))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "node is not available" in proc.stderr


#: A stand-in node answering the driver's stdin protocol (POSIX only).
_STAND_IN_NODE = f"""#!{sys.executable}
import json, os, sys
mode = os.environ["STAND_IN_MODE"]
if mode == "crash":
    print("node: internal error", file=sys.stderr); sys.exit(1)
if mode == "garbage":
    print("not json"); sys.exit(0)
files = json.load(sys.stdin)
out = [{{"path": f["path"], "ok": True}} for f in files]
if mode == "short":
    out = out[:-1]
if mode == "doubled":
    out = [out[0]] + out[:-1]
sys.stdout.write(json.dumps(out))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stand-in for node")
@pytest.mark.parametrize("mode", ["crash", "garbage", "short", "doubled"])
def test_a_node_run_that_did_not_cover_every_file_is_no_verdict(tmp_path, mode):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    node = bin_dir / "node"
    node.write_text(_STAND_IN_NODE, encoding="utf-8", newline="\n")
    node.chmod(0o755)
    src = _tree(tmp_path, {"a.jsx": _OK, "b.jsx": _OK})
    proc = _run("--ci", "--src", str(src),
                env=dict(_ENV, PATH=str(bin_dir), STAND_IN_MODE=mode))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "esbuild-parse did not run" in proc.stderr, proc.stderr


def test_an_empty_tree_is_no_verdict(tmp_path):
    (tmp_path / "src").mkdir()
    proc = _run("--ci", "--src", str(tmp_path / "src"))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "ERROR:" in proc.stderr


def test_a_missing_tree_is_named(tmp_path):
    proc = _run("--ci", "--src", str(tmp_path / "nope"))
    assert proc.returncode == 2
    assert "--src" in proc.stderr and "nope" in proc.stderr


# ── one definition of the build's transform ──────────────────────────────

def test_the_build_reads_the_shared_transform_not_a_copy():
    """build.mjs takes the strip, loader and JSX settings from
    esbuild-shared.mjs: a second copy there is what let the old lint's
    regex and the build's drift apart."""
    build = (PORTAL / "build.mjs").read_text(encoding="utf-8")
    assert "from './esbuild-shared.mjs'" in build
    for copy in ("^---", "jsxImportSource:", "jsx: 'automatic'", "format: 'esm'"):
        assert copy not in build, copy


def test_one_hook_runs_each_jsx_check_in_its_strictest_mode():
    import yaml
    cfg = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = {h["id"]: h for r in cfg["repos"] for h in r.get("hooks", [])}
    assert "jsx-babel-check" not in hooks
    syntax = hooks["jsx-syntax-check"]
    assert "lint_jsx_syntax.py" in syntax["entry"] and "--ci" in syntax["entry"].split()
    line = hooks["jsx-linecount-check"]
    assert "check_jsx_linecount.py" in line["entry"]
    assert {"--ci", "--strict"} <= set(line["entry"].split())
    import re
    files = re.compile(syntax["files"])
    for path in ("tools/portal/src/interactive/tools/x.jsx", "tools/portal/src/a/b.js",
                 "tools/portal/esbuild-shared.mjs", "tools/portal/package-lock.json",
                 "scripts/tools/lint/lint_jsx_syntax.py"):
        assert files.search(path), path
    assert not files.search("tools/portal/src/a/b.css")
