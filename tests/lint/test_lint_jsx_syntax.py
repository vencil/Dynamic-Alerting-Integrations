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
    # With an import/export it is a module, as the build takes it: strict mode.
    "return_in_a_module.js": "export const a = 1;\nreturn a;\n",
    "octal_in_a_module.js": "export const a = 010;\n",
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
    # No import/export of its own: a script to esbuild, which the build wraps
    # and ships, and which then runs — not stricter than the build (#3669
    # review: appending `export {}` made the lint red where the build is not).
    "script_top_level_return.js": "const a = 1;\nreturn a;\n",
    "script_legacy_octal.js": "const a = 010;\n",
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
    n = sum(1 for tree in ("src", "entries") for p in (PORTAL / tree).rglob("*")
            if p.suffix in (".jsx", ".js"))
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
def _fake_esbuild(at, version):
    """A package named esbuild at *at* (node_modules/esbuild) with *version*."""
    d = at / "esbuild"
    d.mkdir(parents=True)
    (d / "package.json").write_text(
        f'{{"name": "esbuild", "version": "{version}", "main": "index.js"}}', encoding="utf-8")
    (d / "index.js").write_text("module.exports = {};\n", encoding="utf-8")


def _locked_version():
    import json
    lock = json.loads((PORTAL / "package-lock.json").read_text(encoding="utf-8"))
    return lock["packages"]["node_modules/esbuild"]["version"]


def _fake_portal(tmp_path):
    portal = tmp_path / "work" / "portal"
    portal.mkdir(parents=True)
    shutil.copy(PORTAL / "esbuild-shared.mjs", portal)
    shutil.copy(PORTAL / "package-lock.json", portal)
    (portal / "package.json").write_text('{"name": "p", "private": true}', encoding="utf-8")
    return portal


def test_an_esbuild_outside_the_portal_is_no_verdict(tmp_path):
    """node finds an esbuild in a parent directory's node_modules (or on
    NODE_PATH): not the lockfile's install, so no verdict — never a pass."""
    if not shutil.which("node"):
        pytest.skip("node is not on PATH")
    portal = _fake_portal(tmp_path)
    _fake_esbuild(tmp_path / "work" / "node_modules", _locked_version())
    proc = _run("--ci", "--portal-dir", str(portal), "--src", str(_tree(tmp_path, {"a.jsx": _OK})))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "esbuild resolved outside" in proc.stderr and "make portal-deps" in proc.stderr


def test_an_esbuild_of_another_version_than_the_lockfile_is_no_verdict(tmp_path):
    """A stale install after a lockfile bump: no verdict, not a pass."""
    if not shutil.which("node"):
        pytest.skip("node is not on PATH")
    portal = _fake_portal(tmp_path)
    _fake_esbuild(portal / "node_modules", "0.0.1")
    proc = _run("--ci", "--portal-dir", str(portal), "--src", str(_tree(tmp_path, {"a.jsx": _OK})))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "esbuild 0.0.1 is installed, the lockfile pins" in proc.stderr


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

#: esbuild options that change how a file is parsed. In build.mjs they may
#: come only from TRANSFORM_OPTIONS (or the shared strip / loader).
_PARSE_KEYS = {"target", "supported", "format", "loader", "jsx", "jsxFactory",
               "jsxFragment", "jsxImportSource", "jsxDev", "jsxSideEffects",
               "tsconfig", "tsconfigRaw", "charset"}


def _config_literal(build: str) -> list[str]:
    import re
    start = build.index("  const config = {")
    end = build.index("\n  };", start)
    body = build[start:end].splitlines()[1:]
    # The literal's own top-level lines (4-space indent), comments dropped.
    return [ln.strip() for ln in body
            if re.match(r"^    \S", ln) and not ln.strip().startswith("//")]


def test_the_build_parses_with_the_shared_transform_only():
    """build.mjs takes the strip, loader and every parse-relevant setting from
    esbuild-shared.mjs, and spreads TRANSFORM_OPTIONS LAST, so no key in the
    config can override it — the lint parses exactly as the build does. Read
    off the config literal's own keys (#3669 review: a string match let
    `target: 'es2020'` after the spread through)."""
    import re
    build = (PORTAL / "build.mjs").read_text(encoding="utf-8")
    assert "from './esbuild-shared.mjs'" in build
    top = _config_literal(build)
    assert top[-1] == "...TRANSFORM_OPTIONS,", top[-1]
    keys = {m.group(1) for ln in top if (m := re.match(r"^(\w+)\s*:", ln))}
    assert not keys & _PARSE_KEYS, keys & _PARSE_KEYS
    assert not re.search(r"\bconfig(\.\w+|\[)\s*=", build), "config mutated after it is built"
    assert "stripFrontmatter(" in build and "loaderFor(" in build
    assert "/^---" not in build, "a second front-matter regex"


@pytest.mark.parametrize("override", ["    target: 'es2020',", "    supported: {},",
                                       "    jsx: 'transform',"])
def test_the_build_guard_sees_an_override(override):
    """The guard's own control: a parse-relevant key in the config is named."""
    import re
    build = (PORTAL / "build.mjs").read_text(encoding="utf-8")
    mutated = build.replace("    bundle: true,", "    bundle: true,\n" + override, 1)
    top = _config_literal(mutated)
    keys = {m.group(1) for ln in top if (m := re.match(r"^(\w+)\s*:", ln))}
    assert keys & _PARSE_KEYS


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
                 "tools/portal/entries/x.entry.jsx", "tools/portal/build.mjs",
                 # Built, not a literal: only matched against the regex,
                 # never read (the path-filter scanner harvests literals).
                 "tools/portal/" + "package.json",
                 "tools/portal/esbuild-shared.mjs", "tools/portal/package-lock.json",
                 "scripts/tools/lint/lint_jsx_syntax.py"):
        assert files.search(path), path
    assert not files.search("tools/portal/src/a/b.css")
