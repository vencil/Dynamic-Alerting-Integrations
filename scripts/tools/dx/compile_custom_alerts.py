#!/usr/bin/env python3
"""Compile Custom Alert recipes → rule-packs/rule-pack-custom-alerts.yaml.

ADR-024 Capability B (#741 S1+S2). Reads `_custom_alerts` declarations from a
conf.d tree, groups them by shape signature, and emits ONE vectorised
`group_left` rule per shape (rule count = shape count, NOT per-tenant fan-out —
preserves the rule-pack O(M) invariant). Tenants never write PromQL.

The generated pack flows through the EXISTING fan-out unchanged (both glob
`rule-pack-*.yaml`): generate_rulepack_configmaps.py → configmap, and
operator_generate.py → PrometheusRule CRD.

Source of declarations (`--config-dir`): defaults to the LIVE exporter tree
`components/threshold-exporter/config/conf.d` — the same canonical source every
gate (Makefile `custom-alerts-compile*` / pre-commit / CI) pins explicitly, and
the tree the committed pack is compiled from since S3b. The source MUST be the
conf.d the exporter serves, or recipe_id will not match emit. The docs example
tree (`rule-packs/recipes/examples/conf.d/`) stays reachable via `--config-dir`.

`--check` regenerates in memory and SEMANTICALLY compares against the committed
pack (via check_rulepack_sync), so a stale / hand-edited pack is a hard failure.

⛔ A compile that produces NOTHING will not overwrite a pack that has rules
(`--allow-empty` to override, refused while recipes are quarantined). A source
file the compiler stops seeing — renamed, moved, or a name its discovery does not
match — is indistinguishable from "the last recipe was removed" at this layer, and
the write is irreversible in the working tree. See `_erases_committed_rules`.

⚠️ What that guard does NOT cover, measured rather than assumed:
  * **Partial loss.** Three tenants, rename one file: rc=0 and that tenant's rules
    are simply absent. `--check` still catches it as drift, and now names what
    regenerating would remove (`_rules_regenerating_would_drop`) — but nothing
    refuses the write.
  * **No pack, no baseline.** The comparison is against the pack on disk, so a
    first compile — a greenfield tree, a fresh `--out`, a deleted pack — has
    nothing to compare and writes whatever it got. Only the zero-shape warning
    speaks up there.
  * **The library entry point.** `build_pack()` has no guard at all; callers that
    import this module and render themselves bypass everything here.

Exit codes:
    0  wrote file (default) / in sync (--check)
    1  drift detected (--check) / refused to erase the committed pack (write)
    2  error (invalid declaration tree, missing source, …)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, "..", "lint"))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
import check_rulepack_sync as sync  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)

try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover
    def try_utf8_stdout() -> None:  # type: ignore
        pass

# import the compiler package (scripts/tools/dx/custom_alerts/)
sys.path.insert(0, _THIS_DIR)
from custom_alerts import loader as _loader  # noqa: E402
from custom_alerts import recipes as _recipes  # noqa: E402
from custom_alerts import shape as _shape  # noqa: E402
from custom_alerts.loader import CustomAlertConfigError  # noqa: E402


PACK_NAME = "custom-alerts"
DEFAULT_CONFIG_REL = "components/threshold-exporter/config/conf.d"
OUT_REL = "rule-packs/rule-pack-custom-alerts.yaml"


def _repo_root() -> Path:
    p = Path(_THIS_DIR).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return p.parent.parent.parent


class _BlockDumper(yaml.SafeDumper):
    """Emit multiline strings as `|` block scalars for readable PromQL exprs."""


def _str_representer(dumper: yaml.Dumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_BlockDumper.add_representer(str, _str_representer)


def _silent_sentinel() -> dict:
    """The SINGLE global (tenant-agnostic) silent-mode sentinel (#741 S7/S8).

    Fires once per (tenant, name) whose recipe declares mode=silent, derived
    straight from the exporter's user_threshold series — so it is injected ONCE
    regardless of shape count (the query already spans every silent recipe in
    the cluster). Alertmanager uses it as an inhibit SOURCE (equal:[tenant,name])
    to suppress that recipe's notification while Prometheus keeps evaluating it
    as an ALERTS series for dashboards. This rides the platform's established
    ADR-003 Sentinel+Inhibit silent paradigm (mirrors TenantSilentWarning/
    Critical) instead of an Alertmanager route-to-null receiver — more
    observable in the AM UI and consistent with the tenant-level tri-state.

    Like every platform sentinel it carries the static component="sentinel"
    routing discriminator (#1095): the sentinel is an inhibit source + AM-UI/
    dashboard surface, NOT a notification, and it still carries a `tenant`
    label — without the discriminator it would fall through to the tenant main
    route (and a matcher-less enforced NOC route) and notify humans. The
    platform-static sentinel-sinkhole route (continue:false, ahead of enforced/
    tenant routes) swallows it; inhibition is unaffected (Alertmanager matches
    inhibit sources against all active alerts regardless of routing).
    """
    return {
        "alert": "CustomRecipeSilent",
        "expr": 'max by(tenant, name) (user_threshold{component="custom", mode="silent"})',
        "labels": {
            "severity": "none",
            "component": "sentinel",
            "tenant": "{{ $labels.tenant }}",
            "name": "{{ $labels.name }}",
        },
        "annotations": {
            "summary": "Custom recipe [{{ $labels.name }}] is silent for {{ $labels.tenant }}",
            "summary_zh": "{{ $labels.tenant }} 的自訂告警 [{{ $labels.name }}] 處於靜默模式",
            "description": ("The recipe is still evaluated and visible as an ALERTS "
                           "series (dashboard-only); its notifications are suppressed "
                           "via inhibit, not deleted."),
            "description_zh": ("此自訂告警仍會評估並可於監控面板（ALERTS series）查看，"
                              "通知經 inhibit 抑制而非刪除。"),
        },
    }


# Platform-authored template actions the compiler ITSELF emits into custom-alert
# rule labels/annotations — the ONLY {{ … }} allowed in a generated custom-alert
# rule. Anything else means tenant-controlled data became Go-template code (the F2
# annotation-injection class), regardless of WHICH field leaked it. Emit-time
# INVARIANT gate (A+ defence-in-depth): shape.py's boundary reject stops the known
# selector-value vector; this catches ANY future field reaching a template context
# without its own guard. Allowed: `{{ $value | printf "%.Nf" }}` and `{{ $labels.X }}`.
_ALLOWED_TEMPLATE_ACTION = re.compile(
    r'\{\{\s*(?:\$value\s*\|\s*printf\s+"%\.[0-9]+f"|\$labels\.[a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}'
)


def _assert_annotations_template_safe(groups: List[dict]) -> None:
    """Fail the compile if any generated label/annotation carries a Go-template
    action OR backtick beyond the platform allowlist. Catches the F2 injection class
    at emit time no matter which field carried it. MUST be kept in lockstep with the
    platform annotations emitted by recipes.py / _silent_sentinel."""
    for g in groups:
        for r in g.get("rules", []):
            fields = dict(r.get("labels") or {})
            fields.update(r.get("annotations") or {})
            for name, val in fields.items():
                residual = _ALLOWED_TEMPLATE_ACTION.sub("", str(val))
                if "{{" in residual or "}}" in residual or "`" in residual:
                    ident = r.get("alert") or r.get("record") or "<rule>"
                    raise CustomAlertConfigError(
                        f"emit-time invariant violation: {name!r} in {ident!r} contains a "
                        f"non-platform Go-template action or backtick — tenant-controlled data "
                        f"must never become template code (F2 injection class). Value: {val!r}"
                    )


def _safe_log(value) -> str:
    """Deprecated alias for :func:`_lib_io.safe_label` (#1538).

    This function WAS the rule — added for #1008 so a malformed tenant id /
    origin / exception text could not inject forged log lines or terminal
    escapes into a quarantine line. #1538 found the same defect in 18 more
    tools and promoted this implementation to the shared output layer.

    ⚠️ The character class DID change in the promotion, and an earlier wording
    here said it did not: the shared rule adds **C1** (``\\x80``–``\\x9f``), so a
    value that reaches this function with ``\\x85`` now becomes ``?`` where the
    pre-#1538 body passed it through. Behaviour change, deliberate, disclosed. The name is kept because callers and
    ``tests/dx/test_compile_custom_alerts.py`` reference it; the body is now a
    delegate so there is exactly ONE definition of the escaping rule in the
    repo rather than a copy per tool.
    """
    return safe_label(value)


def build_pack(config_dir: Path,
               max_custom_recipes: int = _loader.MAX_CUSTOM_RECIPES_DEFAULT) -> dict:
    """Build the rule-pack dict (groups) from a conf.d tree."""
    shapes, per_tenant, skipped = _loader.build_shapes(config_dir, max_custom_recipes=max_custom_recipes)

    recording: List[dict] = []
    alerts: List[dict] = []
    info: List[dict] = []
    for shape in shapes:
        rec, alr = _recipes.emit_shape(shape)
        recording.extend(rec)
        alerts.extend(alr)
        # D1 (ADR-024 §8): a static lifecycle-info series per shape, so SRE can join
        # recipe USAGE to recipe STATUS for a tech-debt burn-down:
        #   count by(recipe_id)(user_threshold{component="custom"})
        #     * on(recipe_id) group_left(recipe, status) custom_recipe_info
        # user_threshold carries recipe_id but not recipe/status, and deriving the
        # recipe type from recipe_id needs a fragile label_replace — this info series
        # supplies recipe + status keyed by the same recipe_id instead.
        info.append({
            "record": "custom_recipe_info",
            "expr": "vector(1)",
            "labels": {
                "recipe_id": shape["recipe_id"],
                "recipe": shape["recipe"],
                "status": _shape.recipe_status(shape["recipe"]),
            },
        })

    groups: List[dict] = []
    if recording:
        groups.append({
            "name": "custom-alerts-normalization",
            "interval": "15s",
            "rules": recording,
        })
    if info:
        # Static metadata (never changes between compiles) → its own group on a
        # slow 1m interval, kept out of the 15s normalize cadence.
        groups.append({
            "name": "custom-alerts-info",
            "interval": "1m",
            "rules": info,
        })
    if alerts:
        # Inject the single global silent sentinel ONCE, ahead of the shape
        # alerts (S7/S8). It is tenant-agnostic — never per-recipe.
        groups.append({"name": "custom-alerts", "rules": [_silent_sentinel()] + alerts})

    # A+ emit-time invariant gate: no tenant-controlled data may have become a
    # Go-template action in any generated label/annotation (F2 defence-in-depth).
    _assert_annotations_template_safe(groups)
    return {
        "groups": groups,
        "_meta": {"shapes": len(shapes), "info": len(info), "per_tenant_counts": per_tenant,
                  "skipped": skipped},
    }


def _render(groups: List[dict]) -> str:
    header = (
        "# ============================================================\n"
        "# custom-alerts Rule Pack — Tenant-authored declarative alerts\n"
        "# GENERATED from _custom_alerts declarations by\n"
        "# scripts/tools/dx/compile_custom_alerts.py — DO NOT EDIT.\n"
        "# Run `make custom-alerts-compile` after editing recipes/conf.d.\n"
        "# Rule count = SHAPE count (vectorized, not per-tenant; ADR-024 §2b).\n"
        "# ============================================================\n"
    )
    body = yaml.dump(
        {"groups": groups},
        Dumper=_BlockDumper,
        sort_keys=False, default_flow_style=False, allow_unicode=True, width=10_000,
    )
    return header + body


def _committed_rules(out_path: Path) -> dict:
    """Rules already in the pack at *out_path*, in the same identity form `--check` uses.

    ⚠️ An unreadable / malformed pack answers {} ON PURPOSE, not by accident. This
    function exists to protect content that is there; a pack that cannot be parsed
    has no content to protect, and regenerating it is the repair. Raising here would
    turn "your committed pack is corrupt" into "the compiler no longer runs", i.e. it
    would take away the only way out.
    """
    if not out_path.exists():
        return {}
    try:
        return sync._extract(sync._groups_from_rulepack(out_path))
    except Exception:
        return {}


def _erases_committed_rules(produced: dict, committed: dict) -> bool:
    """True when writing *produced* would leave the pack with none of its rules.

    ⛔ THE PREDICATE IS "PRODUCED NOTHING", NOT "PRODUCED FEWER". Losing SOME rules
    is a legitimate everyday edit (a tenant retires one recipe), so gating on any
    shrink would fail-red on ordinary work and the guard would be removed. Losing
    ALL of them is the shape that means the compiler saw an empty source — which a
    rename, a move, or a filename its discovery does not match produces just as
    readily as a genuine "the last recipe is gone".

    ⚠️ NOT GUARDED, on purpose and measured: partial loss. Renaming one of three
    tenant files still drops that tenant's alerts silently through this path
    (measured: rc=0, `Compiled 2 shape(s)`, that tenant's rules simply absent).
    `_rules_regenerating_would_drop` is what makes that visible in the message;
    this predicate deliberately does not fire on it.

    ⚠️ NOT GUARDED, and this one is structural: *committed* is read from the pack
    on disk, so when there is no pack there is no baseline and nothing fires. A
    first compile of a tree whose declarations the loader cannot see writes an
    empty pack and every downstream gate agrees with it. `_render_write_summary`
    is the only thing that speaks up in that case, and it only warns.
    """
    return bool(committed) and not produced


def _rules_regenerating_would_drop(produced: dict, committed: dict) -> set:
    """Rule identities the committed pack has that this compile did NOT produce.

    ⛔ THIS IS A DISCLOSURE, NOT A GATE, and the difference is the whole design.
    Regenerating is the right answer to ordinary drift, so a message that forbade
    it whenever anything disappeared would be wrong far more often than right —
    an edited recipe legitimately retires its old rule identity. What is NOT
    acceptable is telling someone to regenerate without saying what regenerating
    removes: measured on a three-tenant tree, renaming one file made `--check` red
    with the plain "run the compiler" remedy, and following it dropped that
    tenant's alerts from the pack, the ConfigMaps and the CRD with rc=0 and every
    gate green. Naming the casualties costs nothing and is always true.
    """
    return set(committed) - set(produced)


def _net_rule_change_note(produced: dict, committed: dict) -> str:
    """What regenerating changes, as −removed / +added — empty when nothing goes.

    ⛔ THE `+ADDED` HALF IS WHAT MAKES THIS READABLE INSTEAD OF NOISE. A rule identity
    encodes the window, the metric and the operator, so an ordinary `window: 5m -> 10m`
    retires four identities and creates four — and a note that reported only the four
    removals was, measured, WORD-FOR-WORD IDENTICAL to the note for a tenant whose file
    had genuinely vanished. A disclosure that cannot tell the everyday case from the
    one that matters gets skimmed past, and then it is not a disclosure. −4 / +4 and
    −4 / +0 are distinguishable at a glance.
    """
    dropped = _rules_regenerating_would_drop(produced, committed)
    if not dropped:
        return ""
    added = set(produced) - set(committed)
    lines = [f"   ⚠ Regenerating: −{len(dropped)} rule(s) the pack has, +{len(added)} it does not.\n"]
    if not added:
        lines.append("     Nothing replaces them — this is a net loss of coverage.\n")
    lines.extend(f"       −{ident}\n" for ident in sorted(dropped))
    lines.append("     If you did not mean to retire those, find out why the compiler no "
                 "longer produces them before regenerating.\n")
    return "".join(lines)


def _rerun_command(args) -> str:
    """A command the reader can paste, carrying the arguments THEY used.

    ⛔ NAMING A BARE FLAG IS NOT AN ESCAPE HATCH. The documented way to run this
    compiler is `make custom-alerts-compile`, whose recipe takes no arguments — so
    "re-run with --allow-empty" is not something the person who hit this can do.
    Either they abandon the documented path and reconstruct the invocation from
    memory, or they hard-wire the flag into the Makefile target, which turns the
    guard off for everyone. Printing the whole command removes that fork.
    """
    # ⛔ THE INTERPRETER AND THE SCRIPT ARE RESOLVED, NOT SPELLED. `python` is the
    # Microsoft Store stub on a stock Windows host (exit 49, no output), every caller
    # in this repo says `python3`, and a repo-relative script path only works from the
    # repo root. Measured on all three: the printed line failed rc=2 or 49.
    parts = [_shell_quote(sys.executable), _shell_quote(Path(__file__).resolve())]
    if args.config_dir:
        parts.append(f"--config-dir {_shell_quote(args.config_dir)}")
    if args.out:
        parts.append(f"--out {_shell_quote(args.out)}")
    if args.max_custom_recipes != _loader.MAX_CUSTOM_RECIPES_DEFAULT:
        parts.append(f"--max-custom-recipes {args.max_custom_recipes}")
    parts.append("--allow-empty")
    return " ".join(parts)


# ⛔ ONE STRING, TWO POLARITIES. The erasing branch must carry this and the ordinary
# drift branch must not; a test that spells the sentence out instead pins the wording,
# so rewording it — or writing it in Chinese, which is this repo's house style for
# user-facing prose — would turn a build red for no behavioural reason. Naming the
# constant lets the tests assert the polarity and lets the prose move freely.
DO_NOT_REGENERATE = ("⛔ Do not regenerate to clear this. Regenerating here deletes "
                     "every custom alert in the pack.")

# The other prohibition. Both are constants for the same reason: every defect this
# guard has shipped so far was one branch's message contradicting itself or blaming
# the wrong cause, and a test can only pair a prohibition with the offer it forbids
# if both have names. See `test_no_message_forbids_and_offers_the_same_thing`.
DO_NOT_ALLOW_EMPTY = ("⛔ Fix those declarations first. Do NOT pass --allow-empty here: "
                      "the source is not empty, and writing an empty pack would drop "
                      "rules that are still declared.")


def _shell_quote(value) -> str:
    """Quote a path for pasting back into a shell — only when it needs it.

    ⚠️ Measured: a `--config-dir` under `C:/…/my conf.d` produced a line that argparse
    rejected with `unrecognized arguments: conf.d` (rc=2). Fail-loud rather than
    destructive, but the whole point of printing the command was that it runs.
    """
    text = str(value)
    return f'"{text}"' if any(c.isspace() for c in text) else text


def _deliberate_empty_offer(args, quarantined: int) -> str:
    """The paste-able way to write an empty pack on purpose — withheld while quarantined.

    ⛔ THIS FUNCTION EXISTS BECAUSE THE FIRST VERSION CONTRADICTED ITSELF. The
    quarantine branch said "Do NOT pass --allow-empty here" and three lines later
    printed a command containing `--allow-empty`, i.e. the same defect this whole
    change is about — a message naming the cheaper, destructive move — reproduced
    one layer down, inside the fix for it. A reader who skims takes the command.
    """
    if quarantined:
        return ""
    return (f"\n   Once you know which of those it is, and only if the declarations really "
            f"are gone, write the empty pack deliberately with\n"
            f"     {_rerun_command(args)}")


def _nothing_compiled_diagnosis(config_dir: Path, quarantined: int) -> str:
    """Why this compile produced no rules — ordered by what to check first.

    ⛔ THE QUARANTINE BRANCH IS NOT A NICETY. #1008 fail-soft drops an invalid
    recipe and compiles the rest, so a single mistyped `window: 5x` in the only
    tenant that declares anything makes `produced` empty while the source still
    declares that recipe. Measured: the first version of this message sent that
    reader looking for a renamed file and offered them the flag that erases the
    pack — a one-character fix answered with a destructive one. The compiler is
    holding `skipped` two dozen lines further up; it can simply say so.

    ⛔ THE QUARANTINE NOTE IS ADDITIVE, NOT A REPLACEMENT, and getting that wrong was
    a regression of its own. The first version early-returned, which swallowed "the
    compiler read: <dir>" — a line the version before it printed unconditionally. A
    rename and a typo can be true at the same time, and the reader who is told only
    about the typo fixes it, recompiles, and lands in the partial-loss path with a
    tick and rc=0. Whose tree was read is never the wrong thing to say.
    """
    head = f"   The compiler read: {config_dir}\n"
    if quarantined:
        return (f"{head}"
                f"   {quarantined} recipe(s) were QUARANTINED above — the source still declares "
                f"them, they just failed to compile.\n"
                f"   {DO_NOT_ALLOW_EMPTY}\n"
                f"   ⚠ Fixing them may still leave fewer rules than the pack has — check the "
                f"count after recompiling.")
    return (f"{head}"
            f"   Check, in this order:\n"
            f"     1. a declaration the compiler stopped seeing — a renamed, moved or deleted\n"
            f"        source file, or a filename its discovery does not match;\n"
            f"     2. a source file whose contents no longer parse (it would be quarantined above);\n"
            f"     3. the last declaration really was removed — that is a legitimate end state.")


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(description="Compile custom-alert recipes → rule pack")
    parser.add_argument("--check", action="store_true",
                        help="verify committed pack matches source (semantic); exit 1 on drift")
    parser.add_argument("--config-dir", default=None,
                        help=f"conf.d tree with _custom_alerts (default: {DEFAULT_CONFIG_REL})")
    parser.add_argument("--out", default=None,
                        help=f"output rule pack path (default: {OUT_REL})")
    parser.add_argument("--max-custom-recipes", type=int,
                        default=_loader.MAX_CUSTOM_RECIPES_DEFAULT,
                        help=f"per-tenant cap on OWN recipes (default: "
                             f"{_loader.MAX_CUSTOM_RECIPES_DEFAULT}; inherited policy uncapped)")
    parser.add_argument("--allow-empty", action="store_true",
                        help="write a pack with no rules over one that has rules "
                             "(refused by default; only when removing the last recipe is intended)")
    args = parser.parse_args()

    repo = _repo_root()
    config_dir = Path(args.config_dir) if args.config_dir else repo / DEFAULT_CONFIG_REL
    out_path = Path(args.out) if args.out else repo / OUT_REL

    if not config_dir.exists():
        print(f"ERROR: config dir not found: {config_dir}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        pack = build_pack(config_dir, max_custom_recipes=args.max_custom_recipes)
    except CustomAlertConfigError as e:
        # surface compile-time validation errors as caller errors (exit 2)
        print(f"ERROR: invalid custom-alert declarations: {e}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    groups = pack["groups"]
    meta = pack["_meta"]

    # #1008 Part B: surface quarantined (fail-soft) recipes LOUDLY. A shared compiler
    # gate must not abort the whole compile on one bad recipe (that blocks every
    # tenant's PR merge — a cross-tenant DoS), so an invalid recipe is dropped and the
    # rest compile. Report each drop to stderr + the pack _meta so it is never silent
    # (a quarantined recipe does not deploy). NOT a hard failure by design.
    skipped = meta.get("skipped", [])
    for s in skipped:
        print(f"  ⚠ custom-alert QUARANTINED (fail-soft, #1008): tenant={_safe_log(s['tenant'])} "
              f"name={s['name']!r} ({_safe_log(s['origin'])}): {_safe_log(s['reason'])}", file=sys.stderr)
    if skipped:
        print(f"  ⚠ {len(skipped)} custom-alert recipe(s) quarantined — compiled the rest. "
              f"A quarantined recipe does NOT deploy; fix it (or it stays dropped).",
              file=sys.stderr)

    # Non-fatal recipe-lifecycle notices (ADR-024 #6): deprecated/eol recipes in
    # use still compile (no silent alert loss); surface them to stderr so a GitOps
    # PR / CI log flags the migration debt without breaking the build.
    for notice in _loader.collect_lifecycle_notices(config_dir):
        print(f"  ⚠ recipe-lifecycle: {notice}", file=sys.stderr)

    # Prerequisite notice (#692 P0③ W3): disk-fill recipes (kubelet_volume_stats_*)
    # depend on cluster-side plumbing the compiler CANNOT verify — a CSI driver
    # implementing NodeGetVolumeStats, a kubelet volume-stats scrape job, and a
    # namespace→tenant relabel (ADR-006 §Addendum). Surface it at author-time so the
    # GitOps author wires it (honest: we INFORM, we do not assert the prereq is met).
    # byo_check.py verifies the live flow; CustomRecipeDiskInert is the runtime backstop.
    if any("kubelet_volume_stats_" in r.get("expr", "")
           for g in groups for r in g.get("rules", [])):
        print("  ⚠ disk-recipe prerequisite: a disk-fill recipe (kubelet_volume_stats_*) "
              "compiled — it only fires if the cluster has CSI NodeGetVolumeStats, a kubelet "
              "volume-stats scrape job, and a namespace→tenant relabel. Verify the live flow "
              "with `byo_check.py prometheus`.", file=sys.stderr)

    # Prerequisite notice (#692 P0④): disk-IOPS recipes (container_fs_*) depend on a
    # cadvisor container_fs scrape + namespace→tenant relabel AND on the storage exposing
    # I/O to cgroup blkio — network volumes (NFS/EFS) bypass blkio and emit 0. It is
    # PER-CONTAINER, not per-PVC. We INFORM at author-time; byo_check.py is the live
    # fidelity gate (no runtime sentinel — an inert IOPS recipe is always platform-side).
    if any("container_fs_" in r.get("expr", "")
           for g in groups for r in g.get("rules", [])):
        print("  ⚠ disk-IOPS-recipe prerequisite: a disk-IOPS recipe (container_fs_*) compiled "
              "— it only fires if cadvisor scrapes container_fs with a namespace→tenant relabel "
              "AND the storage exposes I/O to cgroup blkio (network volumes like NFS/EFS bypass "
              "it → 0). Per-CONTAINER, not per-PVC. Verify the live flow with `byo_check.py "
              "prometheus` after a representative load test.", file=sys.stderr)

    produced = sync._extract(groups)
    committed = _committed_rules(out_path)

    if args.check:
        findings = sync._diff_maps(produced, committed)
        if findings:
            print(f"  ❌ {OUT_REL} drifted from custom-alert source:")
            for f in findings:
                print(f"       {f}")
            # ⛔ THE REMEDY LINE IS BRANCHED, AND THAT IS THE POINT. "Regenerate" is
            # the right answer to ordinary drift and the WRONG answer when the source
            # compiled to nothing — regenerating is then what deletes the pack.
            # Measured: renaming one tenant file to `.yml` turns this gate red, and
            # following the instruction this line used to print unconditionally
            # emptied the shipped pack (7016 -> 447 bytes) and turned the gate green.
            dropped = _rules_regenerating_would_drop(produced, committed)
            if _erases_committed_rules(produced, committed):
                print(f"\n❌ custom-alerts rule pack out of sync — and the source now "
                      f"compiles to NOTHING while the committed pack has "
                      f"{len(committed)} rule(s).\n"
                      f"   {DO_NOT_REGENERATE}\n"
                      f"{_nothing_compiled_diagnosis(config_dir, len(skipped))}"
                      f"{_deliberate_empty_offer(args, len(skipped))}", file=sys.stderr)
            else:
                # ⛔ THE CASUALTY LIST IS THE HALF THAT WAS MISSING. This branch used
                # to say "regenerate" and nothing else, including for partial loss —
                # the same defect one size down. Measured on three tenants: renaming
                # one file landed HERE, and following this line dropped that tenant's
                # alerts from pack, ConfigMaps and CRD at rc=0 with every gate green.
                print(f"\n❌ custom-alerts rule pack out of sync. "
                      f"Run `make custom-alerts-compile` to regenerate.", file=sys.stderr)
                print(_net_rule_change_note(produced, committed), end="", file=sys.stderr)
            return EXIT_VIOLATION
        print(f"✅ custom-alerts rule pack matches source "
              f"({meta['shapes']} shape(s)).")
        return EXIT_OK

    # Display repo-relative when the out path is inside the repo, else show it as
    # given. `relative_to` raises ValueError for an --out outside the repo (or on a
    # different drive on Windows) — without this guard the success line would crash
    # AFTER the file was already written, turning a successful compile into a
    # traceback + nonzero exit (false failure).
    #
    # ⛔ COMPUTED BEFORE THE WRITE, so the refusal below names the pack the same way
    # the success line does. A refusal that prints an absolute path while every other
    # line prints a repo-relative one reads like it is talking about a different file.
    try:
        shown = out_path.relative_to(repo)
    except ValueError:
        shown = out_path

    if _erases_committed_rules(produced, committed) and not args.allow_empty:
        print(f"❌ refusing to write: this compile produced no rules, and "
              f"{shown} currently has {len(committed)}.\n"
              f"   Writing would remove every custom alert from the pack, and the "
              f"working-tree copy is gone once it is written.\n"
              f"{_nothing_compiled_diagnosis(config_dir, len(skipped))}"
              f"{_deliberate_empty_offer(args, len(skipped))}", file=sys.stderr)
        return EXIT_VIOLATION

    # ⛔ THE FLAG IS NOT A UNIVERSAL OVERRIDE. `--allow-empty` says "I mean to ship a
    # pack with no rules"; quarantined recipes say "the source still declares rules,
    # they just did not compile". Those two cannot both be true, and honouring the
    # flag here would answer a mistyped `window:` by deleting every rule in the pack.
    if args.allow_empty and skipped and not produced:
        print(f"❌ refusing to write: --allow-empty says the source has no declarations, "
              f"but {len(skipped)} recipe(s) were quarantined above — the source declares "
              f"them and they failed to compile.\n"
              f"   Fix or remove those declarations; the flag is for a source that is "
              f"genuinely empty.", file=sys.stderr)
        return EXIT_VIOLATION

    out_path.write_text(_render(groups), encoding="utf-8", newline="\n")
    if meta["shapes"]:
        print(f"✅ Compiled {meta['shapes']} shape(s) → {shown}")
    else:
        # ⛔ AN EMPTY COMPILE NEVER GETS A TICK. `✅ Compiled 0 shape(s)` with rc=0 is
        # the exact signature of the incident this guard exists for, and the guard
        # itself cannot fire when there is no pack to compare against — a greenfield
        # tree whose declarations the loader cannot see compiles to nothing on day one
        # and every downstream gate agrees (measured). This line is all that stands
        # between that and silence, so it says what happened rather than congratulating.
        #
        # ⛔ AND IT REUSES THE DIAGNOSIS RATHER THAN GUESSING. Saying "the compiler did
        # not see their declarations" is false when the compiler saw them and threw
        # them out itself — the same misattribution this change already fixed once, one
        # branch over. `skipped` is in hand here too.
        print(f"⚠ Compiled 0 shape(s) → {shown} — the pack now contains no rules.")
        print(_nothing_compiled_diagnosis(config_dir, len(skipped)), file=sys.stderr)
    # ⛔ THE CASUALTY NOTE BELONGS ON THE PATH THAT ACTUALLY DELETES, TOO. It was on
    # `--check` only, i.e. on the branch that changes nothing — while the write that
    # really drops the rules said `✅ Compiled N shape(s)` and named none of them.
    if meta["shapes"]:
        print(_net_rule_change_note(produced, committed), end="", file=sys.stderr)
    if meta["per_tenant_counts"]:
        worst = max(meta["per_tenant_counts"].values())
        print(f"   per-tenant EFFECTIVE recipe counts (own + inherited): "
              f"{meta['per_tenant_counts']} (max={worst}). OWN-recipe cap "
              f"{args.max_custom_recipes} enforced at compile (inherited policy "
              f"is vectorized + uncapped).")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
