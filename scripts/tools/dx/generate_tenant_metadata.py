#!/usr/bin/env python3
"""租戶元資料產生器 — 讀 exporter 整天實際發出的值（da-guard served-values），推斷 owner、tier、routing_channel；expected_exporters 只取 `_expected_exporters` 宣告值。

No `rule_packs` (#4141): once the readers serve the default-armed keys for
every tenant (#3832), a pack inferred from the served keys was the same for
all of them. What a tenant declares is `_expected_exporters` (#4097).

The tenants and every value read here come from `da-guard served-values`
(#2115 0-B/B4), so the tool needs da-guard: `$DA_GUARD_BINARY`, else
`da-guard` on `$PATH` (in a checkout, `make da-guard-build` builds it to
.build/da-guard). da-guard missing or failing, or a file the exporter's load
drops, exits 2 with da-guard's stderr below the ERROR line.

Usage:
    python3 scripts/tools/dx/generate_tenant_metadata.py              # 產生 JSON
    python3 scripts/tools/dx/generate_tenant_metadata.py --config-dir conf.d
    python3 scripts/tools/dx/generate_tenant_metadata.py --output /tmp/out.json
    python3 scripts/tools/dx/generate_tenant_metadata.py --check      # CI drift 偵測
    python3 scripts/tools/dx/generate_tenant_metadata.py --dry-run    # 只印出不寫檔
    python3 scripts/tools/dx/generate_tenant_metadata.py --state state.json  # custom_groups 取自 tenant-api state store

custom_groups come from tenant-api's state store (#2883), as tenant-api reads
it (ADR-036 §12): pass --state with the JSON `tenant-api state show` prints
(`kubectl exec deploy/tenant-api -c tenant-api -- tenant-api state show >
state.json`; `-` reads stdin). The old inputs (a saved ConfigMap, a state
directory) are refused, exit 2, naming that command. A groups record
tenant-api cannot use (a `problem` in the JSON) exits 2 naming the problem —
it is undetermined, never empty. Without --state custom_groups is empty and
stderr says why; a `_groups.yaml` left in conf.d is no longer read
(tenant-api moved it in #2856).
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_VIOLATION, EXIT_CALLER_ERROR, OutputNotSerializable, Report, encode_json  # noqa: E402
from _lib_validation import (  # noqa: E402  (#2137 one presence rule)
    FIELD_NOT_STRING,
    FIELD_SET,
    artifact_file_type,
    is_secret_ref,
    receiver_field_state,
)
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping; #3276)
from _lib_paths import classify_path  # noqa: E402  (#3276)
from _lib_tenant_values import (  # noqa: E402  (#2115 0-B/B4: the exporter's answer)
    exit_on_served_values_error,
    expected_exporter_kind,
    load_whole_day,
    now_at,
    recorded_at,
)
from _lib_io import (  # noqa: E402  (#1789; #3982: the artifact entry)
    check_repo_artifact,
    ensure_dir,
    exit_on_output_write_error,
    exit_on_yaml_file_error,
    write_repo_artifact,
)
from _lib_confd import legacy_state_files  # noqa: E402  (#2883)
from _lib_tenant_state import (  # noqa: E402  (#2883)
    KEY_GROUPS, StateInputError, add_state_argument, load_state)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent
DEFAULT_CONFIG_DIR = REPO_ROOT / "components" / "threshold-exporter" / "config" / "conf.d"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def extract_owner(tenant_config: dict) -> str:
    """Extract owner from _metadata.owner if present."""
    metadata = tenant_config.get("_metadata", {})
    if isinstance(metadata, dict):
        return metadata.get("owner", "")
    return ""


def extract_tier(tenant_config: dict) -> str:
    """Extract tier from _metadata.tier if present."""
    metadata = tenant_config.get("_metadata", {})
    if isinstance(metadata, dict):
        return metadata.get("tier", "")
    return ""


def extract_environment(tenant_name: str, metadata: dict) -> str:
    """Return the environment the tenant's `_metadata` resolves to, or "".

    Not inferred from the tenant name (#2830), for the reason the exporter
    kinds are not inferred (extract_expected_exporters): the exporter's tenant_metadata_info and
    tenant-api's list/search read only `_metadata`, and the portal falls back
    to this file when tenant-api is down, so a `prod-x` tenant with no
    environment would show as production here and as unset there. tenant_name
    is kept for the callers' signature."""
    if isinstance(metadata, dict):
        env = metadata.get("environment", "")
        if isinstance(env, str):
            return env
    return ""


def extract_expected_exporters(declared: Any) -> list[str]:
    """Return the entries the tenant declares in `_expected_exporters`, as
    the exporter reads them and as written (an exporter kind, or
    `kind@cluster`, #4097), in order; [] if none.

    Not inferred from metric keys (#2115 B4): Go reads only the declaration.
    The exporter's tenant_expected_exporter liveness series and tenant-api's
    list/search read only that, and the portal falls back to this file when
    tenant-api is down, so an inferred value here would show the same tenant
    with two different sets of kinds. [] means "not declared", which is also
    "not liveness-monitored"."""
    if isinstance(declared, (list, tuple)):
        return [str(d) for d in declared if isinstance(d, str) and d]
    return []


def extract_tags(metadata: dict) -> list[str]:
    """Extract tags from _metadata.tags if present."""
    if isinstance(metadata, dict):
        tags = metadata.get("tags", [])
        if isinstance(tags, list):
            return [str(t) for t in tags]
    return []


def extract_groups(metadata: dict) -> list[str]:
    """Extract group memberships from _metadata.groups if present."""
    if isinstance(metadata, dict):
        grps = metadata.get("groups", [])
        if isinstance(grps, list):
            return [str(g) for g in grps]
    return []


def extract_routing_channel(tenant_config: dict) -> str:
    """Extract routing channel from _routing.receiver (format: type:url or type:endpoint)."""
    routing = tenant_config.get("_routing", {})
    if not isinstance(routing, dict):
        return ""

    receiver = routing.get("receiver", {})
    if not isinstance(receiver, dict):
        return ""

    recv_type = receiver.get("type", "")
    if not recv_type:
        return ""

    # Format: type:url or type:endpoint. #1560: a URL held in a Secret
    # (a Secret reference) is not known here: the channel reads `type:secret`,
    # never the reference's text.
    def url_channel(value: object) -> str:
        if is_secret_ref(value):
            return f"{recv_type}:secret"
        return f"{recv_type}:{value}" if value else ""

    if recv_type == "webhook":
        return url_channel(receiver.get("url", ""))
    elif recv_type == "email":
        to = receiver.get("to", "")
        return f"{recv_type}:{to}" if to else ""
    elif recv_type == "slack":
        return url_channel(receiver.get("api_url", ""))
    elif recv_type == "teams":
        return url_channel(receiver.get("webhook_url", ""))
    elif recv_type == "pagerduty":
        # The keys are credentials: name the Events API version, never the
        # key. service_key wins when both are set, as it does in Alertmanager.
        # Presence is the routing pipeline's rule (receiver_field_state). A
        # non-string key is a config error the pipeline rejects, so the
        # version cannot be told: "" like every other undeterminable channel.
        sk = receiver_field_state(receiver, "service_key")
        rk = receiver_field_state(receiver, "routing_key")
        if FIELD_NOT_STRING in (sk, rk):
            return ""
        if sk == FIELD_SET:
            return f"{recv_type}:v1"
        if rk == FIELD_SET:
            return f"{recv_type}:v2"
        return ""

    return ""



def count_metrics(tenant_config: dict) -> int:
    """Count non-reserved metric keys: the threshold keys /metrics serves
    for the tenant in some part of the day, inherited ones included (#2115
    0-B/B4, #4109)."""
    count = 0
    for key in tenant_config.keys():
        if not key.startswith("_"):
            count += 1
    return count


def get_git_head_commit() -> str:
    """Get current git HEAD commit hash, or empty if not in git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()[:7]  # Short hash
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


# ---------------------------------------------------------------------------
# Build tenant metadata
# ---------------------------------------------------------------------------
def build_tenant_metadata(config_dir: Path, at: str | None = None,
                          state: Any = None) -> dict[str, Any]:
    """Build the metadata structure from what the exporter serves over the
    whole UTC day (#4109): `at` (RFC3339; None = now) only says whether an
    `expires:` has passed, and `generated` records it, so `--check` can
    re-evaluate at it (`recorded_at`). No field is a state at one instant:
    a tenant's operational mode (silence, maintenance — `recurring:` windows
    included) is tenant-api's `config_derived`, derived when it is asked.

    #2115 0-B/B4: the tenants, their `_metadata` and every key the inference
    reads are `da-guard served-values` (/metrics), not this tool's own read
    of the YAML: tenants in sub-directories and in a root platform file's
    `tenants:`, `_metadata` inherited there, threshold keys inherited from
    `defaults:` — all as the exporter resolves them. Nothing is merged here.

    Raises what `load_whole_day` raises (da-guard missing or failing, a file
    the exporter's load drops, a part of the day /metrics cannot be gathered
    in — named, whatever time it is); a caller must not turn that into an
    empty result. Files the load serves no tenant from, and every line da-guard
    wrote to stderr, are printed to stderr (by `load_whole_day`).

    `state` is a `_lib_tenant_state.TenantState` (custom_groups come from it,
    #2883), or None: custom_groups is then empty.
    """
    tenant_groups = {}
    tenant_metadata = {}

    if at is None:
        at = now_at()
    whole = load_whole_day(config_dir, at=at)
    # The threshold keys served in some part of the day, `_metadata` as the
    # exporter's resolver reads it, `_expected_exporters` as it serves them, and `_routing` as the exporter module's
    # routing resolver reads it (`--config-facts`: read at no instant).
    tenant_configs = {}
    for tid, day in whole.days.tenants.items():
        facts = whole.facts.tenants[tid]
        config: dict[str, Any] = {k: sch for k, sch in day.items()
                                  if any(seg.value is not None for seg in sch.segments)}
        config["_metadata"] = facts.metadata
        config["_expected_exporters"] = list(facts.expected_exporters)
        if facts.routing is not None:
            config["_routing"] = facts.routing
        tenant_configs[tid] = config

    # Process each tenant
    for tenant_name, tenant_config in sorted(tenant_configs.items()):
        if not isinstance(tenant_config, dict):
            continue

        # Extract metadata fields
        metadata = tenant_config.get("_metadata", {})
        environment = extract_environment(tenant_name, metadata)
        region = metadata.get("region", "") if isinstance(metadata, dict) else ""
        domain = metadata.get("domain", "") if isinstance(metadata, dict) else ""

        # v2.5.0: extract extended metadata fields
        expected_exporters = extract_expected_exporters(tenant_config.get("_expected_exporters"))
        tags = extract_tags(metadata)
        group_memberships = extract_groups(metadata)

        # Build tenant entry
        tenant_entry = {
            "environment": environment,
            "region": region,
            "tier": extract_tier(tenant_config),
            "domain": domain,
            "expected_exporters": expected_exporters,
            "owner": extract_owner(tenant_config),
            "routing_channel": extract_routing_channel(tenant_config),
            "metric_count": count_metrics(tenant_config),
            "last_config_commit": get_git_head_commit(),
            "tags": tags,
            "groups": group_memberships,
        }

        # Clean up empty fields (expected_exporters is a list, [] when none)
        for key in ["environment", "region", "domain"]:
            if not tenant_entry[key]:
                tenant_entry[key] = ""

        tenant_metadata[tenant_name] = tenant_entry

        # Track tenant groups by environment
        if environment:
            if environment not in tenant_groups:
                tenant_groups[environment] = {
                    "label": environment.capitalize(),
                    "tenants": [],
                }
            tenant_groups[environment]["tenants"].append(tenant_name)

    # #2883: custom groups live in tenant-api's state store, not conf.d.
    custom_groups = _load_custom_groups(state, config_dir)

    # v2.5.0: Build multi-dimension group views
    dimension_groups = _build_dimension_groups(tenant_metadata)

    return {
        "_comment": "Auto-generated by generate_tenant_metadata.py — DO NOT EDIT",
        "generated": at,
        "generator": "scripts/tools/dx/generate_tenant_metadata.py",
        "tenant_groups": tenant_groups,
        "custom_groups": custom_groups,
        "dimension_groups": dimension_groups,
        "tenant_metadata": tenant_metadata,
        "totals": {
            "tenants": len(tenant_metadata),
            "groups": len(tenant_groups),
        },
    }


def _load_custom_groups(state: Any, config_dir: Path) -> dict:
    """Custom groups from tenant-api's state store (#2883).

    `state` is a `_lib_tenant_state.TenantState`, or None (no --state): the
    groups are then unknown and come back empty (the CLI says so). A
    `_groups.yaml` still in conf.d is named and not read — tenant-api has
    kept groups in its state store since #2856, so that copy is stale.
    """
    for name in legacy_state_files(config_dir):  # Go's `role` (#3206)
        if name == "_groups.yaml":
            print(f"WARN: {safe_label(str(Path(config_dir) / name))} is not read here: "
                  "tenant-api imports it at startup only while its state store has no "
                  "groups record (pass --state)",
                  file=sys.stderr)
    if state is None:
        return {}  # main says so on stderr; generate_platform_data reads no groups
    result = {}
    for gid, gdef in state.groups().items():
        result[gid] = {
            "label": gdef.get("label", gid),
            "description": gdef.get("description", ""),
            "members": [str(m) for m in (gdef.get("members") or [])],
        }
    return result


def _build_dimension_groups(tenant_metadata: dict[str, dict]) -> dict:
    """Build multi-dimension group views from tenant metadata.

    Produces three dimension views: by-domain, by-expected_exporter, by-region.
    Each view maps dimension value → list of tenant IDs. A list-valued field
    (expected_exporters) puts the tenant under each of its values — by KIND
    (#4097): an entry may be `kind@cluster`, and a tenant declaring
    `mariadb@east` and `mariadb@west` is under `mariadb`, once.
    """
    by_kind = {"expected_exporters"}
    dimensions = {
        "by_domain": "domain",
        "by_expected_exporter": "expected_exporters",
        "by_region": "region",
    }
    result: dict[str, dict[str, list[str]]] = {}

    for dim_key, field in dimensions.items():
        groups: dict[str, list[str]] = {}
        for tenant_id, meta in tenant_metadata.items():
            value = meta.get(field, "")
            values = value if isinstance(value, list) else [value]
            if field in by_kind:
                values = dict.fromkeys(expected_exporter_kind(v) for v in values if isinstance(v, str))
            for v in values:
                if v:
                    groups.setdefault(v, []).append(tenant_id)
        # Sort member lists for deterministic output
        for members in groups.values():
            members.sort()
        result[dim_key] = dict(sorted(groups.items()))

    return result


# ---------------------------------------------------------------------------
# --output: atomic replace (#2082) — the shared helper since #2128
# ---------------------------------------------------------------------------
# The #2082 writer that lived here (realpath, private mkstemp tmp, owner /
# mode / xattr carry-over, in-place fallback + WARN for hard links and
# unwritable directories) moved into `_atomic_write.atomic_write_text` so
# there is one implementation, reached since #3982 through the artifact
# entry `_lib_io.write_repo_artifact`; this tool passes its flag so an error
# still ends "check the value given to --output".


def _read_existing_for_check(out: Path) -> tuple[dict | None, str | None]:
    """``(metadata, None)`` or ``(None, why it cannot be compared)`` (#2082).

    Every way the ``--output`` file can be unusable — half-written JSON, a
    non-object top level, non-UTF-8 bytes, a directory, an unreadable file —
    becomes a reason string instead of a traceback, so ``--check`` can tell
    "damaged" apart from "outdated".
    """
    try:
        raw = out.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return None, f"not UTF-8 ({exc.reason} at byte {exc.start})"
    except OSError as exc:
        return None, f"cannot be read ({exc.strerror or exc})"
    try:
        existing = json.loads(raw)
    except ValueError as exc:
        return None, f"not valid JSON ({exc})"
    except RecursionError:
        return None, "not usable JSON (nested too deeply to parse)"
    if not isinstance(existing, dict):
        kind = {list: "an array", str: "a string", bool: "a boolean",
                int: "a number", float: "a number", type(None): "null",
                }.get(type(existing), type(existing).__name__)
        return None, f"top level is {kind}, expected a JSON object"
    return existing, None


def _strip_volatile(data: dict) -> None:
    """Drop what changes with no input changed: the timestamp and the HEAD
    commit (as generate_platform_data's --check does)."""
    data.pop("generated", None)
    tenants = data.get("tenant_metadata")
    if not isinstance(tenants, dict):
        return  # left as is: the comparison reports the file as outdated
    for tenant in tenants.values():
        if isinstance(tenant, dict):
            tenant.pop("last_config_commit", None)


@exit_on_yaml_file_error  # #1654: any other unreadable YAML → rc 2, named
@exit_on_served_values_error  # #2115 0-B/B4: da-guard missing or failing, a dropped file → rc 2, named
@exit_on_output_write_error
def main():
    """CLI entry point: 租戶元資料產生器."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Generate tenant metadata from conf.d/ YAML files",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=DEFAULT_CONFIG_DIR,
        help=f"Config directory (default: {DEFAULT_CONFIG_DIR.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--output",
        # #3982 (owner 2026-11-18): an artifact — tenant-api reads it, so it
        # is replaced whole; `-` is refused (leave --output out for stdout).
        type=artifact_file_type(then=Path),
        help="Output file path (default: stdout)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="CI mode: exit 1 if metadata is outdated, "
             "exit 2 if the --output file is damaged or unreadable",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print JSON to stdout without writing file",
    )
    add_state_argument(  # #3797: a declared document (`-`: stdin)
        parser,
        help="the JSON `tenant-api state show` prints, for custom_groups "
             "(- reads stdin)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Format output as pretty JSON",
    )
    args = parser.parse_args()

    # Validate config directory
    # #3276: the shared judgement of a caller-given path.
    problem = classify_path(args.config_dir, "dir", lang="en", flag="--config-dir")
    if problem is not None:
        word = "not found" if problem.missing else "not usable"
        print(f"ERROR: Config directory {word}: {problem.message}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # Build metadata. --check evaluates at the instant the existing file
    # recorded, not now: the exporter's answer moves with the clock.
    at = None
    if args.check and args.output:
        # #3982: the artifact's preconditions before the FIRST read — a
        # FIFO with no writer would block it, a device read as damaged.
        check_repo_artifact(args.output, flag="--output")
    if args.check and args.output and args.output.exists():
        at = recorded_at(_read_existing_for_check(args.output)[0])
    state = None
    if args.state is not None:   # ⛔ not truthiness: '' is given, and refused (#3797)
        try:
            state = load_state(args, lang="en")  # #3512: this tool's language
        except StateInputError as exc:
            print(f"ERROR: {safe_label(exc)}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
        prob = state.problem(KEY_GROUPS)
        if prob is not None:
            # ADR-036 §12: undetermined, never empty custom_groups.
            print(f"ERROR: --state {safe_label(state.source)}: tenant-api cannot use "
                  f"{KEY_GROUPS} ({safe_label(prob.kind)}): {safe_label(prob.message)} "
                  "— custom_groups cannot be determined", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
    data = build_tenant_metadata(args.config_dir, at=at, state=state)

    # Format output
    if args.check:
        _strip_volatile(data)

    content = encode_json(data) + "\n"

    # Handle output
    if args.dry_run or not args.output:
        _note_without_state(state)
        return Report(data)  # #3216: run() encodes it — the same text as the file

    if args.check:
        if not args.output.exists():
            print(
                f"ERROR: {args.output} does not exist. "
                f"Run without --check first.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VIOLATION)

        existing, damaged = _read_existing_for_check(args.output)
        if damaged is not None:
            # #2082: rc 2, not the "outdated" rc 1 — the file is not an older
            # version of this output, it is not one at all, and a CI consumer
            # must not read a truncated file as ordinary drift.
            print(
                f"ERROR: {safe_label(str(args.output))} is damaged: {damaged}. "
                f"Regenerate it by re-running without --check.",
                file=sys.stderr,
            )
            sys.exit(EXIT_CALLER_ERROR)
        _strip_volatile(existing)
        try:
            existing_str = encode_json(existing) + "\n"
        except OutputNotSerializable:
            # A hand edit JSON reads but cannot carry (NaN / Infinity): the
            # file is not what the generator writes — outdated, not a crash.
            existing_str = None

        if existing_str != content:
            print(
                f"ERROR: {args.output} is outdated.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VIOLATION)

        print(f"OK: {args.output} is up to date.")
        _note_without_state(state)
        return

    # Write output file
    # #3982: the artifact entry — an atomic replace (#2082), so a failed run
    # leaves the previous file whole; a regular file only, mode kept the
    # way a checkout makes it (0644, 0755 for an executable one).
    ensure_dir(args.output.parent, flag="--output")
    write_repo_artifact(args.output, content, flag="--output")

    try:
        display_path = args.output.relative_to(REPO_ROOT)
    except ValueError:
        display_path = args.output
    print(f"✅ Generated {display_path}")
    print(f"   {data['totals']['tenants']} tenants, {data['totals']['groups']} groups")
    _note_without_state(state)


def _note_without_state(state: Any) -> None:
    """Say on stderr that custom_groups is empty for want of --state (#2883).
    Called on the success exits only, so a failing run does not also print
    this NOTE after its ERROR line."""
    if state is None:
        print("NOTE: custom_groups is empty: no --state given "
              "(the JSON `tenant-api state show` prints)",
              file=sys.stderr)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)  # #3216: run() encodes the --json document
