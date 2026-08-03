#!/usr/bin/env python3
"""check_md_yaml_drift.py — Markdown 內 YAML 範例與 Schema 漂移偵測

掃描 docs/ 下 Markdown 文件中的 ```yaml 區塊，送入 tenant-config.schema.json 校驗。
確保文件上的 YAML 範例永遠是合法配置，schema 變更時自動偵測漂移。

用法:
  python3 scripts/tools/lint/check_md_yaml_drift.py              # 顯示報告
  python3 scripts/tools/lint/check_md_yaml_drift.py --ci          # CI 模式（exit 1 if drift）
  python3 scripts/tools/lint/check_md_yaml_drift.py --verbose     # 顯示所有掃描的區塊

需要: pyyaml, jsonschema
"""

import json
import os
import re
import sys
import argparse
from pathlib import Path
from typing import Dict, List, Tuple

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(EXIT_CALLER_ERROR)

try:
    import jsonschema
except ImportError:
    # Import stays optional so `--help` and `--check fences` (which need no
    # schema at all) keep working in an env without it — the same reason
    # check_confd_schema.py injects the module lazily. The schema pass itself
    # now REFUSES to run without it (see _require_jsonschema): this used to be
    # `jsonschema = None  # Graceful degradation`, and because the hook's
    # additional_dependencies listed only pyyaml, that degradation meant the
    # schema check silently validated nothing on every run it ever did.
    jsonschema = None


# A block carrying any of these top-level keys is a PLATFORM file
# (`_defaults.yaml`) and is judged by platform-defaults.schema.json, which also
# permits `tenants:` — so a doc that stacks L0 + a tenant block in one file
# still routes correctly.
PLATFORM_KEYS = {
    "defaults", "state_filters", "optional_overrides", "profiles",
    "max_metrics_per_tenant", "_routing_defaults", "_routing_enforced",
}

# File-header comment naming a YAML file, e.g. `# conf.d/_defaults.yaml`.
# Anchored at column 0: an indented `#` that merely mentions a filename in
# passing is prose, and treating it as a boundary would sever a block
# mid-mapping. Mirrors fileHeaderRe in the Go gate
# (components/threshold-exporter/app/pkg/config/docs_defaults_sample_test.go).
FILE_HEADER_RE = re.compile(r"^#.*\.ya?ml\b")


class MdYamlDriftChecker:
    """掃描 Markdown 中的 YAML code blocks 並驗證 schema 合規性。"""

    # Tenant-shaped markers: keys that only appear INSIDE a tenant's body, so a
    # block carrying one is a tenant snippet even without a `tenants:` wrapper.
    TENANT_CONFIG_MARKERS = {
        "_thresholds", "_routing", "_silent_mode", "_state_maintenance",
        "_routing_defaults", "_routing_enforced", "_threshold_mode",
        "_write_mode", "_write_back_mode",
    }

    def __init__(self, repo_root: str, verbose: bool = False):
        self.repo_root = Path(repo_root).resolve()
        self.verbose = verbose
        self.schema: Dict = {}
        self.platform_schema: Dict = {}
        self._load_schema()

    def _load_schema(self):
        """Load both schemas: tenant-config and platform-defaults."""
        base = self.repo_root / "docs" / "schemas"
        for attr, name in (("schema", "tenant-config.schema.json"),
                           ("platform_schema", "platform-defaults.schema.json")):
            path = base / name
            if not path.exists():
                continue
            try:
                setattr(self, attr, json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError) as e:
                print(f"WARNING: Cannot load {name}: {e}", file=sys.stderr)

    def _extract_yaml_blocks(self, filepath: Path) -> List[Tuple[int, str]]:
        """Extract ```yaml code blocks from a markdown file.

        Returns list of (line_number, yaml_content) tuples.
        """
        blocks = []
        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return blocks

        in_yaml = False
        block_start = 0
        block_lines = []

        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped in ("```yaml", "```yml") and not in_yaml:
                in_yaml = True
                block_start = i
                block_lines = []
            elif stripped == "```" and in_yaml:
                in_yaml = False
                if block_lines:
                    blocks.append((block_start, "\n".join(block_lines)))
            elif in_yaml:
                block_lines.append(line)

        return blocks

    @staticmethod
    def _split_documented_files(block_lines: List[str]) -> List[Tuple[int, str]]:
        """Split a fence that illustrates SEVERAL files into one segment per file.

        Several docs teach inheritance by stacking `# L0 _defaults.yaml`,
        `# L1 finance/_defaults.yaml` and a tenant file into ONE fence. Judged
        whole, such a block is not even valid YAML (a repeated top-level key),
        which says nothing about whether the documented config is right.

        Returns (line_offset, text) pairs so a violation points at the offending
        file rather than at the fence. Mirrors splitConcatenatedFiles in the Go
        gate so the two extractors see the same units.
        """
        segments: List[Tuple[int, str]] = []
        current: List[str] = []
        start = 0
        for i, line in enumerate(block_lines):
            if FILE_HEADER_RE.match(line) and current:
                segments.append((start, "\n".join(current)))
                current, start = [], i
            current.append(line)
        if current:
            segments.append((start, "\n".join(current)))
        return segments

    def _is_tenant_config(self, data) -> bool:
        """Is this block a Vibe config example (platform file or tenant snippet)?

        Widened beyond the tenant markers to include the PLATFORM keys: a block
        that is plainly a `_defaults.yaml` (`defaults:` / `state_filters:` /
        `optional_overrides:`) had no schema oracle at all before, so a typo'd
        top-level key such as `state_flters` passed unnoticed — the Go loader is
        non-strict and cannot see it either.

        ⚠️ Deliberately NOT selecting on a bare top-level `tenants:` yet, though
        that is the obvious next widening. Measured: it would add 99 units and
        surface ~15 failures that are four different things, not one —
        genuinely broken examples (an email receiver with no `smarthost`, same
        class as the hands-on-lab fix), examples that are invalid ON PURPOSE
        (docs/internal/editor-schema-validation.md demonstrates what the editor
        rejects), `...` ellipsis placeholders, and a whole second config model
        (`alerts.threshold` / `receivers` in manage-at-scale + multi-domain).
        Each needs its own call, so folding them in here would mean this gate
        cannot go green — the exact reason it has sat parked. Tracked as the
        follow-up rather than smuggled in.
        """
        if not isinstance(data, dict):
            return False
        for key in data:
            if key in self.TENANT_CONFIG_MARKERS or key in PLATFORM_KEYS:
                return True
        # Nested under a tenant name (`tenants:` omitted in the snippet).
        for value in data.values():
            if isinstance(value, dict):
                for key in value:
                    if key in self.TENANT_CONFIG_MARKERS:
                        return True
        return False

    def _route(self, data: Dict) -> Tuple[Dict, Dict, str]:
        """Pick the schema this block is actually governed by.

        Three shapes, and using one schema for all of them is why this check
        reported 38/38 violations while proving nothing: a `_defaults.yaml`
        block judged against tenant-config.schema.json fails on
        `'tenants' is a required property`, which is a statement about the
        WRONG contract.
        """
        keys = set(data)
        if keys & PLATFORM_KEYS:
            return self.platform_schema, data, "platform"
        if "tenants" in keys:
            return self.schema, data, "tenant-file"
        # A bare tenant body (`_routing:` etc. with no wrapper). Wrap it in a
        # synthetic tenant so it is judged by the real per-tenant subschema
        # instead of being skipped.
        return self.schema, {"tenants": {"example-tenant": data}}, "fragment"

    def run_fences(self) -> int:
        """Fence hygiene: every ```yaml block in docs/ must parse as YAML.

        Deliberately a SEPARATE check from the schema pass below, and wired as
        its own hook, because the two differ on every axis that matters:

        * Coverage — this rule speaks for ALL fenced yaml blocks (472 today);
          the schema pass only speaks for the ~38 that look like tenant config.
        * Concern — this one asks "is the fence honestly labelled?", the other
          asks "does this config match the contract?". Bundling them lets one
          mislabelled directory-tree diagram block an unrelated schema fix.

        Parsed with `safe_load_all`, not `safe_load`: a multi-document stream
        (`---`-separated k8s manifests, a frontmatter example) is perfectly
        valid YAML, and four blocks in docs/ are exactly that. Using the
        single-document loader here would report them as broken and push
        authors toward splitting legitimate manifests apart.

        ⚠️ Known blind spot, stated rather than papered over: "parses as YAML"
        is NECESSARY but not SUFFICIENT for an honest fence tag. A fence holding
        only shell, with no `key:` anywhere, is a valid YAML *scalar* and passes
        here. What it does catch is the mislabelling that actually occurs in
        this repo — directory-tree diagrams, and shell mixed into the same fence
        as a manifest — because those contain colons that collide with the
        surrounding structure. Closing the gap fully needs language detection,
        a different tool with a different false-positive profile; deliberately
        not attempted. Pinned by tests/lint/test_check_md_yaml_fences.py.
        """
        issues = []
        total = 0
        docs_dir = self.repo_root / "docs"
        for md_file in sorted(docs_dir.rglob("*.md")):
            rel_path = str(md_file.relative_to(self.repo_root)).replace(os.sep, "/")
            for line_num, yaml_content in self._extract_yaml_blocks(md_file):
                total += 1
                try:
                    list(yaml.safe_load_all(yaml_content))
                except yaml.YAMLError as e:
                    first = str(e).splitlines()[0]
                    issues.append((rel_path, line_num, first))
                    continue
                if self.verbose:
                    print(f"  OK   {rel_path}:{line_num}")

        print("=" * 60)
        print("MARKDOWN YAML FENCE HYGIENE")
        print("=" * 60)
        print(f"Fenced yaml blocks scanned:  {total}")
        print(f"Blocks that do not parse:    {len(issues)}")
        print()
        if not issues:
            print("✓ Every ```yaml block in docs/ parses as YAML.")
            return EXIT_OK
        for rel_path, line_num, err in issues:
            print(f"  [FENCE] {rel_path}:{line_num}")
            print(f"          {err}")
        print()
        print("A ```yaml fence that is not YAML misleads readers who copy it and")
        print("breaks syntax highlighting. Fix by re-tagging the fence to what it")
        print("actually is (```text for directory trees, ```bash for shell), or by")
        print("splitting mixed content into one fence per language. Separate k8s")
        print("manifests inside ONE block with `---` (a multi-document stream is")
        print("valid here and is what `kubectl apply -f` expects).")
        return EXIT_VIOLATION

    def iter_config_units(self):
        """Yield every documented config unit as (rel_path, line, data).

        One fence can document several files, and one file can be a
        multi-document stream, so a "unit" is a single YAML document inside a
        single documented file — not a fence.

        Blocks that do not parse are NOT reported here: fence hygiene is the
        `--check fences` pass's job, and duplicating it would let a mislabelled
        directory-tree diagram fail the schema gate for a reason that has
        nothing to do with schemas.
        """
        docs_dir = self.repo_root / "docs"
        for md_file in sorted(docs_dir.rglob("*.md")):
            rel_path = str(md_file.relative_to(self.repo_root)).replace(os.sep, "/")
            for line_num, yaml_content in self._extract_yaml_blocks(md_file):
                for offset, text in self._split_documented_files(yaml_content.split("\n")):
                    try:
                        docs = list(yaml.safe_load_all(text))
                    except yaml.YAMLError:
                        continue
                    for data in docs:
                        if data is None or not self._is_tenant_config(data):
                            continue
                        # `line_num` is the ```yaml fence itself; +1 lands on the
                        # segment's first real line. Matches the Go gate's
                        # `b.line + s.offset + 1` exactly — the two gates
                        # reporting the same block at different line numbers is
                        # the first symptom of their extractors drifting apart.
                        yield rel_path, line_num + offset + 1, data

    def run(self) -> int:
        """Execute the schema-conformance pass."""
        if jsonschema is None:
            print("ERROR: jsonschema not installed — the schema check cannot run. "
                  "Install it (pip install jsonschema) or invoke --check fences.",
                  file=sys.stderr)
            return EXIT_CALLER_ERROR
        if not self.schema or not self.platform_schema:
            print("ERROR: could not load docs/schemas/{tenant-config,platform-defaults}"
                  ".schema.json — refusing to report a clean run against no contract.",
                  file=sys.stderr)
            return EXIT_CALLER_ERROR

        issues = []
        checked = 0
        by_kind: Dict[str, int] = {}

        for rel_path, line_num, data in self.iter_config_units():
            schema, doc, kind = self._route(data)
            by_kind[kind] = by_kind.get(kind, 0) + 1
            checked += 1
            try:
                jsonschema.validate(doc, schema)
            except jsonschema.ValidationError as e:
                issues.append({
                    "file": rel_path, "line": line_num, "kind": kind,
                    "error": e.message,
                })
            except jsonschema.SchemaError as e:
                print(f"ERROR: schema itself is invalid: {e}", file=sys.stderr)
                return EXIT_CALLER_ERROR
            if self.verbose:
                print(f"  OK   {rel_path}:{line_num} ({kind})")

        print("=" * 60)
        print("MARKDOWN YAML DRIFT CHECK")
        print("=" * 60)
        print(f"Config units checked:        {checked}")
        for kind in sorted(by_kind):
            print(f"  {kind:<24} {by_kind[kind]}")
        print(f"Schema violations:           {len(issues)}")
        print()

        if not issues:
            print("✓ Every documented config example matches its schema.")
            return EXIT_OK
        for issue in issues:
            print(f"  [SCHEMA] {issue['file']}:{issue['line']} ({issue['kind']})")
            print(f"           {issue['error']}")
        print()
        print("Fix the example, or the schema if the contract genuinely changed.")
        print("Note which contract the block was judged against — a `_defaults.yaml`")
        print("is governed by platform-defaults.schema.json, a tenant snippet by")
        print("tenant-config.schema.json.")
        return EXIT_VIOLATION


def main():
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Check Markdown YAML examples against tenant-config schema"
    )
    parser.add_argument("--ci", action="store_true", help="CI mode: exit 1 on drift")
    parser.add_argument("--verbose", action="store_true", help="Show all scanned blocks")
    parser.add_argument("--repo-root", default=".", help="Repository root (default: .)")
    parser.add_argument(
        "--check", choices=("schema", "fences"), default="schema",
        help="Which check to run: 'schema' (default; tenant-config blocks vs "
             "JSON Schema) or 'fences' (every ```yaml block in docs/ must parse). "
             "They are separate hooks so one cannot mask the other.")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    if not (repo_root / "docs").exists():
        print(f"ERROR: Cannot find docs/ in {repo_root}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    checker = MdYamlDriftChecker(str(repo_root), verbose=args.verbose)
    exit_code = checker.run_fences() if args.check == "fences" else checker.run()

    if args.ci:
        return exit_code
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
