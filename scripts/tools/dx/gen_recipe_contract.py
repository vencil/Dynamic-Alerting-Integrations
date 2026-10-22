#!/usr/bin/env python3
"""
gen_recipe_contract.py — Generate the recipe copies from the one contract.

The custom-alert recipe contract (enums, patterns, bands, lifecycle, SLO
windows and multipliers) is written ONCE, in
components/threshold-exporter/app/pkg/config/recipe_contract.json (#3229).
The exporter reads it through go:embed and tenant-api through pkg/config;
every other copy is GENERATED from it:

  - Schema : the enum / pattern / default / bounds members of
             docs/schemas/tenant-config.schema.json customAlertInstance
             (the rest of each property is hand-written and kept)
  - Portal : tools/portal/src/interactive/tools/_common/data/recipe-enums.json
             and recipe-status.json (imported by recipe-builder)
  - Python : the blocks between the GENERATED markers in
             scripts/tools/dx/custom_alerts/shape.py (the compiler's enums
             and patterns) and scripts/tools/_lib_constants.py (the
             per-tenant custom-alert cap validate-config judges)
  - Recipes: the `status:` line and each param's `enum:` / `default:` in
             rule-packs/recipes/*.yaml (the rest is hand-written and kept)

The portal's severity pair is not the recipe contract's: it is the threshold
grammar's, read from tests/shared/threshold_value_matrix.json (which Go
generates from config.ThresholdSeverities()).

Usage:
  gen_recipe_contract.py [--root DIR] [--check | --update]

Options:
  --root DIR  Repository root the generated files live under (default: the
              repository this script is in)
  --check     Exit 1 if any generated file is stale (for CI)
  --update    Rewrite the stale generated files

Exit codes: 0 in sync (or updated), 1 stale under --check, 2 the contract or
a target cannot be read or is malformed.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
import _lib_compat  # noqa: E402,F401  (import-time side effect; see _lib_compat)
from _lib_compat import require_project_root  # noqa: E402  (#2852)
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from sync_schema import RegistryError, locate, members  # noqa: E402

CONTRACT_REL = "components/threshold-exporter/app/pkg/config/recipe_contract.json"
SEVERITY_REL = "tests/shared/threshold_value_matrix.json"
SCHEMA_REL = "docs/schemas/tenant-config.schema.json"
PORTAL_ENUMS_REL = "tools/portal/src/interactive/tools/_common/data/recipe-enums.json"
PORTAL_STATUS_REL = "tools/portal/src/interactive/tools/_common/data/recipe-status.json"
SHAPE_REL = "scripts/tools/dx/custom_alerts/shape.py"
CONSTANTS_REL = "scripts/tools/_lib_constants.py"
RECIPES_DIR_REL = "rule-packs/recipes"
#: The targets written whole: a missing one is generated, not an error.
WHOLE_FILE_TARGETS = frozenset({PORTAL_ENUMS_REL, PORTAL_STATUS_REL})

PY_BEGIN = "# BEGIN GENERATED recipe contract"
PY_END = "# END GENERATED recipe contract"

_HEADER = (f"GENERATED from {CONTRACT_REL} by scripts/tools/dx/gen_recipe_contract.py "
           "--update — DO NOT EDIT.")

_PROPS = ["definitions", "customAlertInstance", "properties"]


class ContractError(RegistryError):
    """The contract or a target is malformed; the message names what and where."""


def _repo_root():
    """This tree's root, never an enclosing project's (#2852)."""
    return require_project_root(_THIS_DIR)


def load(root):
    """``(contract, severities)`` read from *root*."""
    try:
        contract = json.loads((Path(root) / CONTRACT_REL).read_text(encoding="utf-8"))
        severities = json.loads((Path(root) / SEVERITY_REL).read_text(encoding="utf-8"))["severities"]
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as e:
        raise ContractError(f"cannot read the contract: {e}") from e
    if not isinstance(severities, list) or not severities:
        raise ContractError(f"{SEVERITY_REL}: severities must be a non-empty list")
    return contract, severities


def _inner(pattern):
    """An anchored pattern without its anchors (to embed in an alternation)."""
    if not (pattern.startswith("^") and pattern.endswith("$")):
        raise ContractError(f"pattern {pattern!r} is not anchored")
    return pattern[1:-1]


def op_enum(c, recipe):
    """The ops *recipe* accepts: `==` only on the eq recipes."""
    return [op for op in c["op"]["enum"] if op != "==" or recipe in c["op"]["eq_recipes"]]


# ---------------------------------------------------------------- schema

def schema_members(c):
    """``[(property path under customAlertInstance.properties, member, value)]``."""
    p = c["patterns"]
    slo = c["slo_burn_rate"]
    objective_pattern = f"^({_inner(p['decimal'])}|{slo['objective']['disable']})$"
    return [
        (["recipe"], "enum", [r["name"] for r in c["recipes"]]),
        (["name"], "pattern", p["name"]),
        (["metric"], "pattern", p["metric"]),
        (["denominator_metric"], "pattern", p["metric"]),
        (["capacity_metric"], "pattern", p["metric"]),
        (["objective"], "pattern", objective_pattern),
        (["slo_period"], "enum", slo["slo_period"]["enum"]),
        (["slo_period"], "default", slo["slo_period"]["default"]),
        (["min_events"], "minimum", slo["min_events"]["minimum"]),
        (["min_events"], "maximum", slo["min_events"]["maximum"]),
        (["min_events"], "default", slo["min_events"]["default"]),
        (["horizon"], "enum", c["horizon"]["enum"]),
        (["op"], "enum", c["op"]["enum"]),
        (["op"], "default", c["op"]["default"]),
        (["window"], "pattern", p["window"]),
        (["quantile"], "pattern", p["decimal"]),
        (["selectors", "propertyNames"], "pattern", p["label"]),
        (["selectors_re", "propertyNames"], "pattern", p["label"]),
        (["mode"], "enum", c["mode"]["enum"]),
        (["mode"], "default", c["mode"]["default"]),
        (["for"], "enum", c["for"]["enum"]),
        (["for"], "default", c["for"]["default"]),
        (["group_by", "items"], "enum", c["group_by"]["enum"]),
    ]


def render_schema(text, c):
    """*text* with the contract's members of customAlertInstance set."""
    props = [m[0] for m in members(text, locate(text, _PROPS))]
    fields = [f["name"] for f in c["fields"]]
    if sorted(props) != sorted(fields):
        raise ContractError(
            f"{SCHEMA_REL} customAlertInstance properties {sorted(set(props) ^ set(fields))} "
            f"are not both in the schema and in the contract's fields")
    for path, name, value in schema_members(c):
        text = _set_compact(text, _PROPS + path, name, value)
    return text


def _set_compact(text, obj_path, name, value):
    """*text* with the existing member *name* of the object at *obj_path* set
    to *value*, written on one line the way the schema writes its enums. The
    member must already be there: where a new one goes is a hand-written
    choice, so a missing one is an error, not an insertion."""
    for key, _kstart, vstart, vend in members(text, locate(text, obj_path)):
        if key == name:
            return text[:vstart] + json.dumps(value, ensure_ascii=False) + text[vend:]
    raise ContractError(f"{SCHEMA_REL}: {'.'.join(obj_path)} has no {name!r} member to set")


# ---------------------------------------------------------------- portal

def _dump(doc):
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def render_portal_enums(c, severities):
    slo = c["slo_burn_rate"]
    return _dump({
        "_generated": _HEADER + " severity / severityDefault come from " + SEVERITY_REL + ".",
        "recipe": [r["name"] for r in c["recipes"]],
        "op": c["op"]["enum"],
        "opDefault": c["op"]["default"],
        "eqRecipes": c["op"]["eq_recipes"],
        "horizon": c["horizon"]["enum"],
        "for": c["for"]["enum"],
        "forDefault": c["for"]["default"],
        "mode": c["mode"]["enum"],
        "modeDefault": c["mode"]["default"],
        "severity": severities,
        "severityDefault": severities[0],
        "groupBy": c["group_by"]["enum"],
        "quantileDefault": c["quantile"]["default"],
        "quantileRange": [c["quantile"]["exclusive_minimum"], c["quantile"]["exclusive_maximum"]],
        "forecastCurrentBand": c["forecast"]["current_band"],
        "sloPeriod": slo["slo_period"]["enum"],
        "sloPeriodDefault": slo["slo_period"]["default"],
        "reservedLabels": c["reserved_labels"],
        "patterns": c["patterns"],
    })


def render_portal_status(c):
    return _dump({
        "_generated": _HEADER,
        "statuses": {r["name"]: r["status"] for r in sorted(c["recipes"], key=lambda r: r["name"])},
    })


# ---------------------------------------------------------------- Python

def _py(v):
    return json.dumps(v, ensure_ascii=False)


def _py_re(pattern):
    if '"' in pattern or pattern.endswith("\\"):
        raise ContractError(f"pattern {pattern!r} cannot be written as a raw string")
    return f're.compile(r"{pattern}")'


def _py_tuple(items):
    body = ", ".join(_py(x) for x in items)
    return f"({body},)" if len(items) == 1 else f"({body})"


def _py_fset(items):
    return "frozenset({" + ", ".join(_py(x) for x in items) + "})"


def render_python(c):
    p = c["patterns"]
    slo = c["slo_burn_rate"]
    statuses = "".join(f"    {_py(r['name'])}: {_py(r['status'])},\n" for r in c["recipes"])
    slugs = ", ".join(f"{_py(k)}: {_py(v)}" for k, v in c["op"]["slug"].items())
    windows = "".join(f"    {_py(t['severity'])}: ({_py(t['long'])}, {_py(t['short'])}, "
                      f"{t['min_events_factor']}),\n" for t in slo["tiers"])
    lines = [
        f"{PY_BEGIN} — scripts/tools/dx/gen_recipe_contract.py --update writes this",
        f"# block from {CONTRACT_REL}. DO NOT EDIT.",
        f"RECIPES = {_py_tuple([r['name'] for r in c['recipes']])}",
        f"RECIPE_LIFECYCLE = {_py_fset(c['lifecycle'])}",
        "RECIPE_STATUS = {\n" + statuses + "}",
        f"_NAME_RE = {_py_re(p['name'])}",
        f"_METRIC_RE = {_py_re(p['metric'])}",
        f"_LABEL_RE = {_py_re(p['label'])}",
        f"_WINDOW_RE = {_py_re(p['window'])}",
        f"_QUANTILE_RE = {_py_re(p['decimal'])}",
        f"_TEMPLATE_METACHARS = {_py_tuple(c['template_metachars'])}",
        "RESERVED_LABELS = frozenset({\n"
        + "".join(f"    {_py(x)},\n" for x in c["reserved_labels"]) + "})",
        f"OP_SLUG = {{{slugs}}}",
        f"DEFAULT_OP = {_py(c['op']['default'])}",
        f"_EQ_RECIPES = {_py_fset(c['op']['eq_recipes'])}",
        f"ALLOWED_FOR = {_py_fset(c['for']['enum'])}",
        f"DEFAULT_FOR = {_py(c['for']['default'])}",
        f"ALLOWED_HORIZON = {_py_fset(c['horizon']['enum'])}",
        f"ALLOWED_GROUP_BY = {_py_fset(c['group_by']['enum'])}",
        f"DEFAULT_QUANTILE = {_py(c['quantile']['default'])}",
        f"QUANTILE_RANGE = ({c['quantile']['exclusive_minimum']}, {c['quantile']['exclusive_maximum']})",
        f"OBJECTIVE_RANGE = ({slo['objective']['exclusive_minimum']}, {slo['objective']['exclusive_maximum']})",
        f"OBJECTIVE_DISABLE = {_py(slo['objective']['disable'])}",
        f"ALLOWED_SLO_PERIOD = {_py_fset(slo['slo_period']['enum'])}",
        f"DEFAULT_SLO_PERIOD = {_py(slo['slo_period']['default'])}",
        f"SLO_MIN_EVENTS_DEFAULT = {slo['min_events']['default']}",
        f"SLO_MIN_EVENTS_MIN = {slo['min_events']['minimum']}",
        f"SLO_MIN_EVENTS_MAX = {slo['min_events']['maximum']}",
        "# severity -> (long window, short window, bad-event floor multiplier)",
        "SLO_BURN_WINDOWS = {\n" + windows + "}",
        f"_FORECAST_CURRENT_BAND = {c['forecast']['current_band']}",
        PY_END,
    ]
    return "\n".join(lines) + "\n"


def render_constants(c):
    return "\n".join([
        f"{PY_BEGIN} — scripts/tools/dx/gen_recipe_contract.py --update writes this",
        f"# block from {CONTRACT_REL}. DO NOT EDIT.",
        f"MAX_CUSTOM_RECIPES: Final[int] = {c['custom_alert_cap']}",
        PY_END,
    ]) + "\n"


def splice_python(text, block, rel=SHAPE_REL):
    """*text* with the GENERATED block replaced by *block*."""
    if text.count(PY_BEGIN) != 1 or text.count(PY_END) != 1:
        raise ContractError(f"{rel}: expected one {PY_BEGIN!r} and one {PY_END!r} line")
    start = text.index(PY_BEGIN)
    end = text.index("\n", text.index(PY_END)) + 1
    return text[:start] + block + text[end:]


# ---------------------------------------------------------------- recipes

_STATUS_LINE = re.compile(r"^status:.*$", re.M)
_PARAM_LINE = re.compile(r"^  ([a-z_]+):(\s*)\{(.*)$")
_ENUM_TOKEN = re.compile(r"enum: \[[^\]]*\]")
_DEFAULT_TOKEN = re.compile(r'default: (?:"[^"]*"|[^,}\s]+)')
_REQUIRED_TOKEN = re.compile(r"required: (?:true|false),\s*")


def _yaml_flow(v):
    return json.dumps(v, ensure_ascii=False)


def param_spec(c, recipe, param):
    """``(enum or None, default or None)`` the contract gives *param* of *recipe*."""
    slo = c["slo_burn_rate"]
    return {
        "op": (op_enum(c, recipe), c["op"]["default"]),
        "mode": (c["mode"]["enum"], c["mode"]["default"]),
        "for": (c["for"]["enum"], c["for"]["default"]),
        "horizon": (c["horizon"]["enum"], None),
        "quantile": (None, c["quantile"]["default"]),
        "slo_period": (slo["slo_period"]["enum"], slo["slo_period"]["default"]),
        "min_events": (None, slo["min_events"]["default"]),
    }.get(param, (None, None))


def _set_token(line, token_re, rendered, after_re):
    """*line* with the token *token_re* matches replaced by *rendered*, or
    inserted after the *after_re* match when absent."""
    if token_re.search(line):
        return token_re.sub(lambda _m: rendered, line, count=1)
    m = after_re.search(line)
    if not m:
        raise ContractError(f"cannot place {rendered!r} in {line!r}")
    return line[:m.end()] + rendered + ", " + line[m.end():]


def render_recipe_yaml(text, c, rel):
    name = Path(rel).stem
    recipe = next((r for r in c["recipes"] if r["name"] == name), None)
    if recipe is None:
        raise ContractError(f"{rel}: {name!r} is not a recipe in the contract")
    status = (f"status: {recipe['status']}   # lifecycle: {'|'.join(c['lifecycle'])} "
              f"(ADR-024 #6; SSOT = {CONTRACT_REL})")
    if len(_STATUS_LINE.findall(text)) != 1:
        raise ContractError(f"{rel}: expected exactly one top-level status: line")
    text = _STATUS_LINE.sub(lambda _m: status, text, count=1)
    fields = {f["name"] for f in c["fields"]}
    out, in_params = [], False
    for line in text.split("\n"):
        if line.startswith("params:"):
            in_params = True
        elif in_params and line and not line.startswith(" "):
            in_params = False
        m = _PARAM_LINE.match(line) if in_params else None
        if m:
            param = m.group(1)
            if param not in fields:
                raise ContractError(f"{rel}: param {param!r} is not a field of the contract")
            enum, default = param_spec(c, name, param)
            if default is not None:
                line = _set_token(line, _DEFAULT_TOKEN, f"default: {_yaml_flow(default)}", _REQUIRED_TOKEN)
            if enum is not None:
                anchor = _DEFAULT_TOKEN if default is not None else _REQUIRED_TOKEN
                line = _set_token(line, _ENUM_TOKEN, f"enum: {_yaml_flow(enum)}", re.compile(anchor.pattern + r",\s*"))
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------- main

def targets(root, c, severities):
    """``[(relpath, render(current_text) -> wanted_text)]``."""
    out = [
        (SCHEMA_REL, lambda t: render_schema(t, c)),
        (PORTAL_ENUMS_REL, lambda _t: render_portal_enums(c, severities)),
        (PORTAL_STATUS_REL, lambda _t: render_portal_status(c)),
        (SHAPE_REL, lambda t: splice_python(t, render_python(c))),
        (CONSTANTS_REL, lambda t: splice_python(t, render_constants(c), CONSTANTS_REL)),
    ]
    yamls = sorted((Path(root) / RECIPES_DIR_REL).glob("*.yaml"))
    names = {p.stem for p in yamls}
    missing = sorted({r["name"] for r in c["recipes"]} - names)
    if missing:
        raise ContractError(f"{RECIPES_DIR_REL}: no governance yaml for {missing}")
    for p in yamls:
        rel = f"{RECIPES_DIR_REL}/{p.name}"
        out.append((rel, lambda t, rel=rel: render_recipe_yaml(t, c, rel)))
    return out


def run(root, update):
    """Stale relpaths; writes the stale files when *update*."""
    c, severities = load(root)
    stale = []
    for rel, render in targets(root, c, severities):
        path = Path(root) / rel
        try:
            current = path.read_text(encoding="utf-8") if path.exists() or rel not in WHOLE_FILE_TARGETS else ""
        except (OSError, UnicodeDecodeError) as e:
            raise ContractError(f"{rel}: cannot read: {e}") from e
        wanted = render(current)
        if wanted != current:
            stale.append(rel)
            if update:
                path.parent.mkdir(parents=True, exist_ok=True)
                # newline="\n": force LF on every platform. Without it,
                # Path.write_text uses os.linesep (CRLF on Windows), which the
                # eol=lf gitattribute hides from the working-tree status and
                # from --check (read_text normalizes CRLF→LF) — but esbuild
                # reads the raw bytes, so a CRLF portal copy taints the
                # bundle's sourcemap and re-hashes the shared chunk.
                path.write_text(wanted, encoding="utf-8", newline="\n")
    return stale


def main(argv=None):
    """CLI entry point: generate (--update) or check (--check) the copies."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=None,
                        help="repository root (default: the repository this script is in)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="exit 1 if any generated file is stale (for CI)")
    mode.add_argument("--update", action="store_true",
                      help="rewrite the stale generated files")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else _repo_root()
    try:
        stale = run(root, args.update)
    except RegistryError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    if not stale:
        print(f"OK: generated recipe copies match {CONTRACT_REL}")
        return EXIT_OK
    verb = "UPDATED" if args.update else "STALE"
    for rel in stale:
        print(f"{verb}: {rel}")
    if args.update:
        return EXIT_OK
    print("Run: python3 scripts/tools/dx/gen_recipe_contract.py --update", file=sys.stderr)
    return EXIT_VIOLATION if args.check else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
