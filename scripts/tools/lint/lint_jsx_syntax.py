#!/usr/bin/env python3
"""lint_jsx_syntax.py — Parse every portal source file the way the portal build does.

Each ``*.jsx`` / ``*.js`` under ``tools/portal/src`` and ``tools/portal/entries``
(the build's entry points) is handed to esbuild — the
copy ``tools/portal/package-lock.json`` pins, the one ``build.mjs`` bundles
with — through the transform the build applies: the front-matter strip, the
loader and the JSX settings all come from ``tools/portal/esbuild-shared.mjs``,
the one definition both read (#3669). A file this lint passes is one the
build parses; there is no second parser whose version or rules could drift.

No stricter than the build: a file with no ``import`` / ``export`` of its own
is a script to esbuild (a top-level ``return``, a legacy octal literal are
accepted), as the bundle treats it — esbuild wraps such a file, and its output
runs (measured in #3669's review). The lint answers "does the build parse
this", nothing more.

The esbuild is the lockfile's, checked: it must resolve under the portal's own
``node_modules`` (not a parent directory's, not ``NODE_PATH``), and its
version must be the one ``package-lock.json`` names — a stale install after a
lockfile bump is no verdict, not a pass.

The parse runs in node, located and judged through ``_lib_engine``
(``("node", "esbuild-parse")``). No node, no esbuild under
``tools/portal/node_modules`` or another version than the lockfile's (run
``make portal-deps``), or a node run that
does not return one result per file is no verdict: exit 2, with or without
``--ci`` — never a green run that skipped the parse. A file that is not UTF-8
is a finding without being sent to node.

Before #3669 this was ``lint_jsx_babel.py``, which parsed with
``@babel/standalone`` — a parser the browser stopped running (TD-030z) — at a
version pinned in the tool, and carried checks for that runtime's quirks.
The line-count guard it also carried is ``check_jsx_linecount.py`` (no node).

Usage:
    python3 scripts/tools/lint/lint_jsx_syntax.py        # report: findings printed, exit 0
    python3 scripts/tools/lint/lint_jsx_syntax.py --ci   # exit 1 on a finding

Exit codes:
    0 = every file parses (or, without --ci, findings were printed)
    1 = --ci and at least one file does not parse (or is not UTF-8)
    2 = no verdict: node missing, esbuild not installed under the portal or not
        the lockfile's version, the node run did not return one result per
        file, or no source file was found
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402
import _lib_engine  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
PORTAL_DIR = PROJECT_ROOT / "tools" / "portal"
SUFFIXES = (".jsx", ".js")

#: The node driver (ESM). It loads the lockfile's esbuild from the portal's
#: own node_modules and the build's shared settings from esbuild-shared.mjs;
#: reads ``[{path, source}]`` on stdin; prints one ``{path, ok[, error]}`` per
#: file. An esbuild it cannot load is exit 3 with the remedy on stderr.
NODE_DRIVER = r"""
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { readFileSync, realpathSync } from 'node:fs';
import { join, sep } from 'node:path';

const portal = process.env.DA_PORTAL_DIR;
const modules = join(portal, 'node_modules');
const remedy = ' — run `make portal-deps` (npm ci in tools/portal)\n';
const fail = (why) => { process.stderr.write(why + remedy); process.exit(3); };
const require = createRequire(join(portal, 'package.json'));
let resolved;
try {
  resolved = realpathSync(require.resolve('esbuild'));
} catch (e) {
  fail(`esbuild is not installed under ${modules}`);
}
// The lockfile's esbuild, and only the portal's own: node would also find one
// in a parent directory or on NODE_PATH, at whatever version (#3669 review).
if (!resolved.startsWith(realpathSync(portal) + sep + 'node_modules' + sep)) {
  fail(`esbuild resolved outside ${modules} (${resolved})`);
}
const lock = JSON.parse(readFileSync(join(portal, 'package-lock.json'), 'utf8'));
const want = lock.packages && lock.packages['node_modules/esbuild']
  && lock.packages['node_modules/esbuild'].version;
const have = JSON.parse(readFileSync(join(modules, 'esbuild', 'package.json'), 'utf8')).version;
if (!want || have !== want) {
  fail(`esbuild ${have} is installed, the lockfile pins ${want || 'none'}`);
}
const esbuild = require('esbuild');
const shared = await import(pathToFileURL(join(portal, 'esbuild-shared.mjs')).href);
const files = JSON.parse(readFileSync(0, 'utf8'));
const results = [];
for (const f of files) {
  try {
    await esbuild.transform(shared.stripFrontmatter(f.source), {
      ...shared.TRANSFORM_OPTIONS,
      loader: shared.loaderFor(f.path),
      sourcefile: f.path,
      logLevel: 'silent',
    });
    results.push({ path: f.path, ok: true });
  } catch (e) {
    const first = (e.errors && e.errors[0]) || { text: String(e.message || e).split('\n')[0] };
    const at = first.location ? `${first.location.line}:${first.location.column}: ` : '';
    results.push({ path: f.path, ok: false, error: at + first.text });
  }
}
process.stdout.write(JSON.stringify(results));
"""


def _sources(src: Path) -> list[Path]:
    return sorted(p for p in src.rglob("*") if p.suffix in SUFFIXES and p.is_file())


def _label(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _no_verdict(why: str) -> int:
    print(f"ERROR: {why}", file=sys.stderr)
    return EXIT_CALLER_ERROR


def main() -> int:
    """CLI entry point: parse every portal source file the way the portal build does."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Parse the portal's JSX/JS sources with the build's esbuild")
    parser.add_argument("--ci", action="store_true", help="Exit 1 on a file that does not parse")
    parser.add_argument("--portal-dir", default=str(PORTAL_DIR),
                        help="the portal package: its node_modules/esbuild and "
                             "esbuild-shared.mjs (default: tools/portal)")
    parser.add_argument("--src", default=None,
                        help="the tree to check (default: <portal-dir>/src and "
                             "<portal-dir>/entries, the build's entry points)")
    args = parser.parse_args()

    import _lib_input
    import _lib_paths
    portal = Path(args.portal_dir)
    trees = [Path(args.src)] if args.src is not None else [portal / "src", portal / "entries"]
    _lib_paths.require_dir(portal, lang="en", flag="--portal-dir")
    for tree in trees:
        _lib_paths.require_dir(tree, lang="en", flag="--src" if args.src is not None else None)
    src = trees[0]

    findings: list[dict] = []
    files: list[dict] = []
    for path in [p for tree in trees for p in _sources(tree)]:
        label = _label(path)
        try:
            source = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            findings.append({"path": label, "ok": False, "error": f"not UTF-8 ({exc.reason})"})
            continue
        files.append({"path": label, "source": source})
    if not files and not findings:
        raise _lib_input.refuse_empty(_label(src), "a .jsx or .js file", lang="en")

    if files:
        node = _lib_engine.locate("node")
        if isinstance(node, _lib_engine.NoVerdict):
            return _no_verdict(f"{node.reason}; the JSX syntax check did not run")
        env = dict(os.environ, DA_PORTAL_DIR=str(portal.resolve()))
        result = _lib_engine.run(node, "esbuild-parse",
                                 ["--input-type=module", "-e", NODE_DRIVER],
                                 stdin_text=json.dumps(files), env=env, timeout=120,
                                 expect={"paths": [f["path"] for f in files]})
        if isinstance(result, _lib_engine.NoVerdict):
            return _no_verdict(result.message())
        findings += [r for r in result.data if not r["ok"]]

    total = len(files) + sum(1 for f in findings if f["error"].startswith("not UTF-8"))
    if not findings:
        print(f"✓ All {total} portal source files parse (esbuild, as the build does)")
        return 0
    print(f"✗ {len(findings)} portal source file(s) do not parse:\n")
    for f in sorted(findings, key=lambda r: r["path"]):
        print(f"  {f['path']}:{f['error']}" if f["error"][:1].isdigit()
              else f"  {f['path']}: {f['error']}")
    print(f"\nSummary: {total - len(findings)}/{total} files OK")
    return 1 if args.ci else 0


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
