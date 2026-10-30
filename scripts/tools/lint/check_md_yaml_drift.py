#!/usr/bin/env python3
"""check_md_yaml_drift.py — Markdown 內 YAML 範例與 Schema 漂移偵測

掃描 docs/ 下 Markdown 文件中的 ```yaml 區塊：conf.d 範例送入 tenant-config /
platform-defaults schema 校驗、內嵌的 k8s 物件送入真正的 CRD schema 校驗，並要求
每個 ```yaml 區塊都是它的讀者讀得了的 YAML。確保文件上的 YAML 範例永遠是合法配置，
schema 變更時自動偵測漂移。

用法:
  python3 scripts/tools/lint/check_md_yaml_drift.py              # 顯示報告
  python3 scripts/tools/lint/check_md_yaml_drift.py --ci          # CI 模式（exit 1 if drift）
  python3 scripts/tools/lint/check_md_yaml_drift.py --verbose     # 顯示所有掃描的區塊
  python3 scripts/tools/lint/check_md_yaml_drift.py --check fences|crd

Every check reads the documented examples from ONE inventory
(scripts/tools/_lib_doc_yaml_inventory.py, #3416): which fence is which file,
which consumer reads it, its reading or its refusal, its declaration and its
exemption are decided there and nowhere here. A fence says what it is with an
attribute list (```` ```yaml {.confd} ````, ``{.k8s}``,
``{.illustrative reason="…"}``, ``{.confd .expect-refused=<kind>}``,
``{.confd .expect-invalid}``; the module docstring has the closed vocabulary).

需要: jsonschema（schema / crd）；da-guard（$DA_GUARD_BINARY 或 $PATH：每個區塊
預設由 `da-guard parse-tree` 讀）、da-crdecode（k8s 物件與 Helm values）、宣告了
da-read 讀者的區塊另需 da-read。本工具不自己解析任何 YAML。
"""

import json
import os
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
import _lib_parse_tree  # noqa: E402
import _lib_consumer_read  # noqa: E402
import _lib_doc_yaml_inventory as inventory  # noqa: E402  (#3416: the one inventory)

try:
    import jsonschema
except ImportError:
    # Import stays optional so `--help` and `--check fences` (which need no
    # schema at all) keep working in an env without it — the same reason
    # check_confd_schema.py injects the module lazily. The schema pass itself
    # REFUSES to run without it (see `run`): this used to be
    # `jsonschema = None  # Graceful degradation`, and because the hook's
    # additional_dependencies listed only pyyaml, that degradation meant the
    # schema check silently validated nothing on every run it ever did.
    jsonschema = None


# ── `--check crd` scope ──────────────────────────────────────────────────────
# Where the vendored CRD schemas live. Produced by
# scripts/tools/dx/generate_crd_schemas.py from pinned upstream CRDs; see
# docs/schemas/crd/SOURCES.yaml for why upstream rather than a catalog.
CRD_SCHEMA_DIR = ("docs", "schemas", "crd")

# apiVersion groups whose objects are NOT cluster resources: these are apiserver
# CONFIG FILE formats, passed with --audit-policy-file / --admission-control-
# config-file. `kubectl apply` never sees them, so "the API server would reject
# this" — the failure this gate speaks for — cannot happen to them. Listed with
# the reason so a later reader does not "fix" the gap by adding schemas.
NON_CLUSTER_GROUPS = {
    "audit.k8s.io",              # Policy — kube-apiserver audit policy file
    "apiserver.config.k8s.io",   # AdmissionConfiguration — admission config file
}

# Built-in k8s kinds are deliberately OUT of scope for now: validating them
# needs the whole upstream schema bundle, and their required-field surface is
# thin. Kept as an explicit list rather than a "looks built-in" predicate
# because the list rots SAFELY — a group missing from it is treated as a CRD and
# fails closed asking for a schema, which is loud. A suffix rule would do the
# opposite and silently wave through anything ending in `.k8s.io`.
BUILTIN_GROUPS = {
    "",                              # core (`apiVersion: v1`)
    "apps", "batch", "policy", "autoscaling",
    "networking.k8s.io", "rbac.authorization.k8s.io", "storage.k8s.io",
    "apiextensions.k8s.io", "scheduling.k8s.io", "coordination.k8s.io",
}


class MdYamlDriftChecker:
    """掃描 Markdown 中的 YAML code blocks 並驗證 schema 合規性。"""

    def __init__(self, repo_root: str, verbose: bool = False):
        self.repo_root = Path(repo_root).resolve()
        self.verbose = verbose
        self.schema: Dict = {}
        self.platform_schema: Dict = {}
        self._inventory = None
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

    def inventory(self) -> "inventory.Inventory":
        """The docs' inventory, built once. Raises when a reader cannot
        answer (ParseTreeError / ConsumerReadError): nothing is judged then."""
        if self._inventory is None:
            self._inventory = inventory.build(self.repo_root)
        return self._inventory

    def _inventory_or_exit(self):
        """(inventory, None), or (None, EXIT_CALLER_ERROR) after naming the
        reader that did not answer."""
        try:
            return self.inventory(), None
        except _lib_parse_tree.ParseTreeError as e:
            print(f"ERROR: the documented YAML examples are read by `da-guard "
                  f"parse-tree`, which did not answer: {e}", file=sys.stderr)
        except _lib_consumer_read.ConsumerReadError as e:
            print(f"ERROR: cannot read the docs' examples the way their consumer does "
                  f"(da-crdecode for k8s objects, the way kubectl does; da-read for a "
                  f"declared Prometheus / Alertmanager file): {e}", file=sys.stderr)
        return None, EXIT_CALLER_ERROR

    def _route(self, data: Dict) -> Tuple[Dict, Dict, str]:
        """Pick the schema this block is actually governed by.

        Three shapes (`inventory.confd_kind`), and using one schema for all of
        them is why this check reported 38/38 violations while proving nothing:
        a `_defaults.yaml` block judged against tenant-config.schema.json fails
        on `'tenants' is a required property`, which is a statement about the
        WRONG contract.
        """
        kind = inventory.confd_kind(data)
        if kind == "platform":
            return self.platform_schema, data, kind
        if kind == "tenant-file":
            return self.schema, data, kind
        # A bare tenant body (`_routing:` etc. with no wrapper). Wrap it in a
        # synthetic tenant so it is judged by the real per-tenant subschema
        # instead of being skipped.
        return self.schema, {"tenants": {"example-tenant": data}}, "fragment"

    def run_fences(self) -> int:
        """Fence hygiene: every ```yaml block in docs/ is one its reader reads.

        Every error the inventory finds is reported here: an attribute outside
        the closed vocabulary or written without braces, the retired comment
        marker, a declared consumer refusing its block, an `.expect-refused`
        that does not hold, and an undeclared block the conf.d reader
        (parse-tree, the default reader) refuses for a reason other than a top
        level that is not a mapping — a syntax error included (a mislabelled
        directory tree, shell mixed into a manifest).

        Deliberately a SEPARATE check from the schema pass, and wired as its
        own hook, because the two differ on every axis that matters:

        * Coverage — this rule speaks for every fenced yaml block in docs/;
          the schema pass speaks only for the conf.d examples. Both totals are
          printed by the run itself, so neither is restated here.
        * Concern — this one asks "is the fence honestly labelled?", the other
          asks "does this config match the contract?". Bundling them lets one
          mislabelled directory-tree diagram block an unrelated schema fix.

        ⚠️ Known blind spot, stated rather than papered over: "a reader reads
        it" is NECESSARY but not SUFFICIENT for an honest fence tag. A fence
        holding only shell, with no `key:` anywhere, is a YAML *scalar*: its
        top level is not a mapping, which tells no conf.d file, so it passes
        here. What it does catch is the mislabelling that actually occurs in
        this repo — directory-tree diagrams, and shell mixed into the same
        fence as a manifest — because those contain colons that collide with
        the surrounding structure. Pinned by tests/lint/test_check_md_yaml_fences.py.
        """
        inv, rc = self._inventory_or_exit()
        if inv is None:
            return rc
        errors = inv.errors()
        by_consumer: Dict[str, int] = {}
        for u in inv.units:
            if u.exemption is not None or u.errors:
                continue
            label = (f"{u.consumer} ({u.how})" if u.consumer
                     else "no consumer (parse-tree)" if u.read
                     else "no consumer, not a mapping")
            by_consumer[label] = by_consumer.get(label, 0) + 1
            if self.verbose:
                print(f"  OK   {u.where} ({label})")

        print("=" * 60)
        print("MARKDOWN YAML FENCE HYGIENE")
        print("=" * 60)
        print(f"Documented units scanned:    {len(inv.units)}")
        for label in sorted(by_consumer):
            print(f"  {label:<36} {by_consumer[label]}")
        inventory.print_exemptions(inv)
        print(f"Fence errors:                {len(errors)}")
        print()
        if not errors:
            print("✓ Every ```yaml block in docs/ is read by its reader.")
            return EXIT_OK
        for path, line, err in errors:
            print(f"  [FENCE] {path}:{line}")
            print(f"          {err}")
        print()
        print("A ```yaml fence its reader cannot read misleads readers who copy it.")
        print("Fix the text; re-tag a fence that is not YAML (```text for a directory")
        print("tree, ```bash for shell); or declare what the block is, with attributes")
        print("after the language: ```yaml {.k8s} (a `---`-separated manifest),")
        print("{.confd .expect-refused=<kind>} (a refused file shown on purpose) or")
        print("{.illustrative reason=\"…\"} (no gate judges it; the reason is printed).")
        return EXIT_VIOLATION

    def run_crd(self) -> int:
        """Every k8s object embedded in docs/ must satisfy its real CRD schema.

        The defect class this speaks for is a reader copying a block and having
        the API server reject it (#1353: an ArgoCD Application with no
        `spec.project`, which the CRD makes required). Nothing covered it — the
        original was found by a human doing a category sweep by hand.

        The objects are the inventory's `k8s` units — declared `{.k8s}`, or an
        undeclared mapping with a string `apiVersion` and `kind` — as kubectl
        reads them (da-crdecode, ADR-036 §12). A unit kubectl's reading
        refuses is an inventory error, reported by `--check fences`.

        ⛔ A missing schema is a VIOLATION, not a skip. That is the whole design
        of this pass. Measured three times over while building it: the stripped
        `example/prometheus-operator-crd/` variant, and datreeio's CRDs-catalog,
        BOTH lack `AlertmanagerConfig` v1beta1 — the exact version six blocks in
        docs/ declare, and the version carrying four real defects. Under a
        skip-on-missing rule this gate would have reported a clean run while
        validating none of them. `-ignore-missing-schemas` is kubeconform's name
        for that trap; this pass has no such flag.

        ⛔ Validation is against the FULL openAPIV3Schema, not just the
        top-level required fields. That shortcut was prototyped and rejected on
        measurement: the four AlertmanagerConfig defects live at
        `spec.receivers[].slackConfigs[].apiURL`, four levels down, and a
        required-only pass reported 0 violations on the same inputs — green, and
        blind to every defect present.

        False positives were the stated risk (docs abbreviate). Measured at
        adoption: 31 in-scope objects, 4 violations, all four genuine. The
        escape hatch if one ever appears is `{.illustrative reason="…"}`; no
        block needed it.
        """
        if jsonschema is None:
            print("ERROR: jsonschema not installed — the CRD check cannot run.",
                  file=sys.stderr)
            return EXIT_CALLER_ERROR
        inv, rc = self._inventory_or_exit()
        if inv is None:
            return rc
        base = self.repo_root.joinpath(*CRD_SCHEMA_DIR)
        issues: List[Dict] = []
        checked = 0
        out_of_scope: Dict[str, int] = {}
        for unit in inv.judged("k8s"):
            for data in unit.documents:
                if not isinstance(data, dict):
                    continue
                api_version, kind = data.get("apiVersion"), data.get("kind")
                if not isinstance(api_version, str) or not isinstance(kind, str):
                    continue
                group, _, version = api_version.rpartition("/")
                if group in NON_CLUSTER_GROUPS:
                    label = f"{api_version} {kind} (not a cluster object)"
                    out_of_scope[label] = out_of_scope.get(label, 0) + 1
                    continue
                if group in BUILTIN_GROUPS:
                    label = f"{api_version} {kind} (built-in, out of scope)"
                    out_of_scope[label] = out_of_scope.get(label, 0) + 1
                    continue
                schema_path = base / group / f"{kind}_{version}.json"
                if not schema_path.exists():
                    issues.append({
                        "file": unit.path, "line": unit.line,
                        "what": f"{api_version} {kind}",
                        "error": (f"no vendored schema at "
                                  f"{'/'.join(CRD_SCHEMA_DIR)}/{group}/{kind}_{version}.json "
                                  f"— add {{group: {group}, kind: {kind}, "
                                  f"version: {version}}} to docs/schemas/crd/SOURCES.yaml "
                                  f"and run `make crd-schemas`"),
                    })
                    continue
                try:
                    schema = json.loads(schema_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as e:
                    print(f"ERROR: cannot read {schema_path}: {e}", file=sys.stderr)
                    return EXIT_CALLER_ERROR
                checked += 1
                errors = sorted(jsonschema.Draft7Validator(schema).iter_errors(data),
                                key=lambda err: list(err.path))
                for err in errors:
                    where = "/".join(str(x) for x in err.path) or "<root>"
                    issues.append({
                        "file": unit.path, "line": unit.line,
                        "what": f"{api_version} {kind}",
                        "error": f"{where}: {err.message}",
                    })
                if self.verbose and not errors:
                    print(f"  OK   {unit.where} ({api_version} {kind})")

        print("=" * 60)
        print("MARKDOWN EMBEDDED CRD OBJECTS")
        print("=" * 60)
        print(f"CRD objects validated:       {checked}")
        inventory.print_exemptions(inv)
        # Printed, never silent: a gate that drops inputs quietly reads as
        # "covered everything" when it did not.
        for label in sorted(out_of_scope):
            print(f"  out of scope: {label:<52} {out_of_scope[label]}")
        unread = [u for u in inv.units if u.consumer == "k8s" and u.errors]
        print(f"k8s units not judged here:   {len(unread)} (inventory errors: --check fences)")
        print(f"Schema violations:           {len(issues)}")
        print()
        if not issues:
            print("✓ Every k8s object embedded in docs/ matches its CRD schema.")
            return EXIT_OK
        for issue in issues:
            print(f"  [CRD] {issue['file']}:{issue['line']} ({issue['what']})")
            print(f"        {issue['error']}")
        print()
        print("A reader copies these blocks verbatim. A block the API server would")
        print("reject is a broken instruction, not a stylistic problem — fix the")
        print("example against the CRD, or re-vendor the schema if upstream really")
        print("changed (`make crd-schemas`, then review the diff).")
        return EXIT_VIOLATION

    def _receiver_checks(self, data: Dict, kind: str) -> List[Tuple[Dict, Dict, str]]:
        """Hold every `receiver:` to the one contract the generator enforces.

        platform-defaults.schema.json accepts `^_routing` via an EMPTY
        patternProperties schema, so a top-level `_routing_defaults:` /
        `_routing_enforced:` body is validated by nothing — measured: a receiver
        with a flat `receiver_type`, a `webhook_url` and an invented key passed
        clean, and it is the same defect class as the tenant-side one this gate
        was built to catch.

        Validating the whole routing block against `definitions.routing` would
        be wrong (`_routing_enforced` legitimately carries `enabled:` and
        `match:`, which that definition forbids). What IS shared is the receiver:
        `_grar_routes.py` funnels all four shapes — tenant override (:91),
        profile-substituted (:210), platform-enforced (:257) and tenant primary
        (:395) — through the same `build_receiver_config`. One consumer, one
        contract, so the receiver is checked wherever it appears.

        ⚠️ Boundary, measured rather than assumed. platform-defaults.schema.json
        leaves 11 fields loose. Two had a cross-schema oracle available and are
        now used: `tenants` (above) and the receiver (here). Of the rest,
        `profiles` appears in 2 documented units but tenant-config.schema.json
        has no `profiles` definition to check it against — writing one would be
        inventing a contract, not enforcing an existing one. The other six
        (`_metadata`, `_routing_profile`, `_silent_mode`, `_severity_dedup`,
        `_namespaces`, `_profile` at platform top level) appear in ZERO
        documented units, so no machinery is built for them.
        """
        if not isinstance(data, dict):
            return []
        receiver_schema = {
            "$ref": "#/definitions/receiver",
            "definitions": self.schema.get("definitions", {}),
        }
        out: List[Tuple[Dict, Dict, str]] = []
        for key, value in data.items():
            if not (isinstance(key, str) and key.startswith("_routing")):
                continue
            if isinstance(value, dict) and isinstance(value.get("receiver"), dict):
                out.append((receiver_schema, value["receiver"], f"{kind}:{key}.receiver"))
        return out

    def run(self) -> int:
        """Execute the schema-conformance pass over the inventory's conf.d units.

        A unit is a conf.d example when it is declared `{.confd}` or, undeclared,
        when parse-tree's reading has the shape of one (`inventory.is_confd_example`).
        Every unit of a `{.confd .expect-invalid}` fence must yield at least
        one violation, and none of its violations is reported; a unit that
        yields none is the violation (#3416 review: judged per fence, one wrong
        unit hid every violation of a second, really broken one). A fence
        that documents a correct file beside the counter-example is two fences.
        """
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
        # #2164: platform-defaults.schema.json `$ref`s the tenant schema's
        # routing definitions; jsonschema does not fetch that file itself.
        # #2232: the SAME registry builder as check_confd_schema, which also
        # proves every cross-file `$ref` resolves before any block is judged
        # (unresolved used to be a traceback at rc 1, or silence when no block
        # reached the `$ref`).
        import check_confd_schema
        try:
            registry = check_confd_schema.checked_schema_registry(
                self.schema, self.platform_schema)
        except check_confd_schema.UnresolvableSchemaRef as e:
            print(f"ERROR: docs/schemas: {e}", file=sys.stderr)
            return EXIT_CALLER_ERROR

        inv, rc = self._inventory_or_exit()
        if inv is None:
            return rc
        # an `.expect-invalid` unit → the violations found in it (on purpose)
        expected: Dict[Tuple[str, int], List[str]] = {}
        first_line: Dict[Tuple[str, int], str] = {}
        for unit in inv.judged("confd"):
            data = unit.documents[0]
            schema, doc, kind = self._route(data)
            by_kind[kind] = by_kind.get(kind, 0) + 1
            checked += 1
            # A platform file may legitimately carry a `tenants:` block, and
            # platform-defaults.schema.json leaves that field EXPLICITLY loose
            # ("guarded by tenant-config.schema.json when in a tenant file").
            # Routing on the platform key alone therefore left every tenant body
            # inside a composite example validated by nothing — measured: a flat
            # `receiver_type` with an invented key, the exact defect class this
            # gate exists to catch, passed clean. The two schemas are
            # complementary here as well, so a composite is held to BOTH.
            checks = [(schema, doc, kind)]
            if kind == "platform" and isinstance(data.get("tenants"), dict):
                checks.append((self.schema, {"tenants": data["tenants"]},
                               "platform+tenants"))
            checks.extend(self._receiver_checks(data, kind))
            if unit.expect_invalid:
                expected.setdefault((unit.path, unit.line), [])
                first_line[(unit.path, unit.line)] = unit.text.split("\n", 1)[0]
            for active_schema, active_doc, active_kind in checks:
                try:
                    jsonschema.validate(active_doc, active_schema,
                                        registry=registry)
                except jsonschema.ValidationError as e:
                    if unit.expect_invalid:
                        expected[(unit.path, unit.line)].append(
                            f"{unit.where} ({active_kind}): {e.message}")
                        continue
                    issues.append({
                        "file": unit.path, "line": unit.line, "kind": active_kind,
                        "error": e.message,
                    })
                except jsonschema.SchemaError as e:
                    print(f"ERROR: schema itself is invalid: {e}", file=sys.stderr)
                    return EXIT_CALLER_ERROR
            if self.verbose:
                print(f"  OK   {unit.where} ({kind})")
        for (path, line), found in sorted(expected.items()):
            if not found:
                issues.append({
                    "file": path, "line": line, "kind": "expect-invalid",
                    "error": (f"the fence is declared {{.confd .expect-invalid}}, but this "
                              f"unit ({first_line[(path, line)]!r}) meets its schema: every "
                              "unit of such a fence must be wrong. A correct file shown "
                              "beside the counter-example goes in a fence of its own; a "
                              "block that only shows a reading is "
                              "{.illustrative reason=\"…\"}"),
                })

        not_judged = [u for u in inv.units
                      if u.errors and (u.consumer in (None, "confd"))]
        print("=" * 60)
        print("MARKDOWN YAML DRIFT CHECK")
        print("=" * 60)
        print(f"Config units checked:        {checked}")
        inventory.print_exemptions(inv)
        for kind in sorted(by_kind):
            print(f"  {kind:<24} {by_kind[kind]}")
        print(f"Counter-example units (.expect-invalid): {len(expected)}")
        for (path, line), found in sorted(expected.items()):
            if found:
                print(f"    {path}:{line} — {len(found)} violation(s) on purpose")
        print(f"Units not judged here:       {len(not_judged)} "
              "(inventory errors: --check fences)")
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
        print("tenant-config.schema.json. A counter-example wrong on purpose is")
        print("declared ```yaml {.confd .expect-invalid}.")
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
        "--check", choices=("schema", "fences", "crd"), default="schema",
        help="Which check to run: 'schema' (default; conf.d examples vs "
             "JSON Schema), 'fences' (every ```yaml block in docs/ is read by its "
             "reader, and every fence attribute is one this tool knows), "
             "or 'crd' (every embedded k8s object vs its real CRD schema). "
             "They are separate hooks so one cannot mask the other.")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    if not (repo_root / "docs").exists():
        print(f"ERROR: Cannot find docs/ in {repo_root}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    checker = MdYamlDriftChecker(str(repo_root), verbose=args.verbose)
    dispatch = {"fences": checker.run_fences, "crd": checker.run_crd}
    exit_code = dispatch.get(args.check, checker.run)()

    if args.ci:
        return exit_code
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
