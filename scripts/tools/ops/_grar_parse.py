"""Configuration loading + parsing for generate_alertmanager_routes.

PR-3a (v2.8.0) extracted these helpers out of generate_alertmanager_routes.py
to bring the main file under the line-count cap. All symbols are re-exported
from generate_alertmanager_routes for backwards-compatible test imports.

Functions:
  _parse_platform_config(...)   → defaults / _routing_defaults / profiles / policies
  _parse_tenant_overrides(...)  → per-tenant _routing / _severity_dedup / _metadata
  _parse_config_files(dir)      → walk YAML files → raw parsed dict
  _merge_tenant_routing(...)    → 4-layer merge (defaults → profile → tenant)
  load_tenant_configs(dir)      → orchestrate the full pipeline (public entry)
"""
from __future__ import annotations

import os
import sys

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import is_disabled as _is_disabled  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402
from _lib_confd import warn_nested  # noqa: E402

from _grar_merge import (  # noqa: E402
    _substitute_tenant,
    merge_routing_with_defaults,
)
from _grar_validate import (  # noqa: E402
    POLICY_ERROR_PREFIX,
    _validate_profile_refs,
    check_domain_policies,
    validate_tenant_keys,
)

# ADR-007 --strict fail-open closure: filenames whose content carries the
# domain policies. If such a file is unparseable, or a domain_policies
# block sits in a differently named file, the policies silently vanish —
# strict mode must surface that instead of staying green.
_POLICY_FILENAMES = ("_domain_policy.yaml", "_domain_policy.yml")


def _drop_unusable_policy(fname: str, reason: str, remedy: str, result: dict,
                          warning: str | None = None) -> None:
    """Record content this reader had to drop, then warn — in one place.

    ⛔ There is more than one way for the domain policies to fall on the
    floor: the file does not decode, it does not parse, it parses into
    something that is not a mapping, or it is a fine mapping whose
    ``domain_policies:`` value is not one. **Every one of them ends with
    zero policies enforced**, which is precisely what ADR-007 ``--strict``
    exists to refuse to do quietly — so every one of them has to leave a
    record here. Before #1448 only the "does not parse" branch did.

    Measured on ``generate-routes --validate --strict`` over an otherwise
    clean conf.d, with only ``_domain_policy.yaml`` varying (a routing
    config that the policy would reject; ``cd4dbd05`` is this branch's
    base)::

        _domain_policy.yaml            base rc   this branch
        absent (control)                    0        0   ← rc=0 is reachable
        valid, and violated                 1        1
        syntax error                        1        1   ← the loud sibling
        whole file is a list / scalar   1 (crash)    1
        not UTF-8                       1 (crash)    1
        `domain_policies:` is a list        0 ← !     1
        `domain_policies:` is null          0 ← !     1

    ⛔ The two rows marked ← ! printed ``OK: all configs valid``, on the
    base and on every release before it. They are **not** a regression this
    branch introduced; the crash rows are what this branch had to convert,
    and converting a crash into a skip without this bookkeeping is exactly
    how a crash becomes a silent pass. (An abandoned first attempt at
    #1448 did precisely that — it is not in this history, so do not go
    looking for the silent-pass state in ``git log``.)

    ⚠️ One shape is deliberately still not recorded, unchanged from before
    #1448: a policy file that parses to *nothing* — ``None`` (empty file,
    or every entry commented out) or an empty mapping ``{}``. An
    intentionally empty policy file is a legal config today, so closing
    that is a behaviour change rather than a fail-open fix, and it is
    tracked separately — see the follow-up on #1448.

    ⚠️ Two more readers disagree with this one, both measured, neither
    closed here:

    * **Nested files.** ``check_yaml_syntax`` walks ``conf.d/`` recursively
      and this loop is flat by design (ADR-016 hierarchy is reported by
      ``warn_nested``), so ``conf.d/team-a/beta.yaml`` with a list at its top
      level is ``validate-config`` rc=1 and ``generate-routes --validate
      --strict`` rc=0 ``OK: all configs valid``. Same class as the row above,
      one directory deeper — tracked with the enumeration mismatch, not
      fixed here, because the fix is in the shared enumerator.
    * **The write plane.** ``tenant-api``'s ``internal/policy`` unmarshals a
      null ``domain_policies:`` to a nil map, replaces it with an empty one
      and returns no error, so ``CheckWrite`` permits every write for the
      exact shape this function has just promoted to a blocking ``--strict``
      ERROR. Read from source; not exercised here.

    ⛔ "Nothing" means exactly those two. It did NOT for one round: the
    caller's ``if not data: continue`` ran *before* the shape check, so
    every falsy non-mapping (``[]``, ``0``, ``false``) fell through the gap
    between this ⚠️ and the table above — the table said such a file exits
    1, the ⚠️ said only "nothing" is exempt, and both were wrong for that
    slice. Measured: a body of ``[]`` left ``--strict`` at exit 0 with zero
    policies enforced while ``validate-config`` called the same file a
    non-mapping and exited 1.

    ⛔ *reason* and *remedy* are written by the branch that knows why the
    content dropped, not by the consumer that renders them. The consumer
    used to append one fixed sentence — "fix: repair the YAML syntax" —
    which is the right instruction for exactly one of these causes and
    sends the reader of any other one hunting for a syntax error in a file
    whose syntax is fine.
    """
    if fname in _POLICY_FILENAMES:
        detail = f"{fname}: {reason} — fix: {remedy}"
        # setdefault, matching the sibling below: this helper is also
        # reachable from _parse_platform_config, which tests call with a
        # hand-built result dict.
        result.setdefault("policy_file_errors", []).append(
            detail.replace("\n", " "))
    print(warning or f"  WARN: skip {fname}: {reason}", file=sys.stderr)


def _parse_platform_config(data: dict, fname: str, result: dict) -> None:
    """Extract platform-level config from a _ prefixed YAML file.

    Handles: defaults keys, _routing_defaults, _routing_enforced,
    routing_profiles (ADR-007), domain_policies (ADR-007).
    Mutates *result* in place.
    """
    is_defaults_file = os.path.basename(fname).startswith("_")

    # Collect defaults keys for schema validation
    if isinstance(data.get("defaults"), dict):
        result["defaults_keys"].update(data["defaults"].keys())

    # #1189 / TRK-337: keys the platform RECOGNISES but supplies no value for
    # (the runtime half of the registry's `tier: optional_overrides`). They
    # widen the membership universe validate_tenant_keys checks against, so a
    # tenant can finally set them — which is precisely why they are honoured
    # ONLY from _ prefixed files. A tenant naming its own keys here would be
    # self-authorising past this very check; Go strips it from tenant-owned
    # files for the same reason (applyBoundaryRules).
    if "optional_overrides" in data:
        if is_defaults_file:
            raw = data["optional_overrides"]
            if isinstance(raw, list):
                # ⚠️ Non-string entries are dropped LOUDLY. Go decodes this
                # field straight into []string, so the same bytes that cost
                # Python one list entry cost Go the entire file: a mapping
                # element (a stray trailing colon is enough) is an unmarshal
                # error, and the whole platform block is skipped — taking
                # `defaults:` with it. Dropping quietly here would leave CI
                # green on the config that empties the write plane.
                bad = [k for k in raw if not isinstance(k, str)]
                if bad:
                    print(f"  WARN: optional_overrides in {fname} has "
                          f"{len(bad)} non-string entr{'y' if len(bad) == 1 else 'ies'} "
                          f"({bad!r}) — ignored here, but the Go exporter fails "
                          "the whole file on them", file=sys.stderr)
                result["optional_override_keys"].update(
                    k for k in raw if isinstance(k, str))
            elif raw is not None:
                print(f"  WARN: optional_overrides in {fname} must be a list, "
                      "ignoring", file=sys.stderr)
        else:
            print(f"  WARN: optional_overrides in {fname} ignored "
                  "(platform-scoped; only allowed in _ prefixed files)",
                  file=sys.stderr)

    # Extract _routing_defaults (only from _ prefixed files)
    if "_routing_defaults" in data:
        if is_defaults_file:
            result["routing_defaults"] = data["_routing_defaults"]
        else:
            print(f"  WARN: _routing_defaults in {fname} ignored "
                  "(only allowed in _ prefixed files)", file=sys.stderr)

    # v1.7.0: Extract _routing_enforced (only from _ prefixed files)
    if "_routing_enforced" in data:
        if is_defaults_file:
            raw = data["_routing_enforced"]
            if isinstance(raw, dict) and raw.get("enabled", False):
                result["enforced_routing"] = raw
            elif isinstance(raw, dict) and not raw.get("enabled", False):
                pass  # explicitly disabled → None
            else:
                print(f"  WARN: _routing_enforced in {fname} must be a dict "
                      "with 'enabled: true', ignoring", file=sys.stderr)
        else:
            print(f"  WARN: _routing_enforced in {fname} ignored "
                  "(only allowed in _ prefixed files)", file=sys.stderr)

    # v2.1.0 ADR-007: Extract routing_profiles (only from _routing_profiles.yaml)
    if "routing_profiles" in data:
        rp_fname = os.path.basename(fname)
        if rp_fname in ("_routing_profiles.yaml", "_routing_profiles.yml"):
            profiles = data["routing_profiles"]
            if isinstance(profiles, dict):
                result["routing_profiles"].update(profiles)
            else:
                print(f"  WARN: routing_profiles in {fname} must be a dict, ignoring",
                      file=sys.stderr)
        else:
            print(f"  WARN: routing_profiles in {fname} ignored "
                  "(only allowed in _routing_profiles.yaml)", file=sys.stderr)

    # v2.1.0 ADR-007: Extract domain_policies (only from _domain_policy.yaml)
    if "domain_policies" in data:
        dp_fname = os.path.basename(fname)
        if dp_fname in _POLICY_FILENAMES:
            policies = data["domain_policies"]
            if isinstance(policies, dict):
                result["domain_policies"].update(policies)
            else:
                # #1448: the file decodes, parses, and is a mapping — and
                # every domain policy still ends up on the floor. Same
                # consequence as an unparseable file, so it goes through the
                # same bookkeeping; before this it printed a WARN nobody
                # reads and `--strict` exited 0 with `OK: all configs valid`.
                _drop_unusable_policy(
                    dp_fname,
                    f"'domain_policies:' must be a mapping of domain name to "
                    f"policy, got {type(policies).__name__} — the whole block "
                    f"is ignored",
                    f"indent each domain under `domain_policies:` in "
                    f"{dp_fname}",
                    result,
                    warning=f"  WARN: domain_policies in {fname} must be a "
                            f"dict, ignoring")
        else:
            # setdefault: tests may call this helper with a hand-built dict.
            result.setdefault("policy_misplacements", []).append(fname)
            print(f"  WARN: domain_policies in {fname} ignored "
                  "(only allowed in _domain_policy.yaml)", file=sys.stderr)


def _parse_tenant_overrides(tenant: str, overrides: dict, result: dict) -> None:
    """Extract per-tenant config from a tenant overrides dict.

    Handles: tenant keys, _routing_profile (ADR-007), _severity_dedup,
    _metadata, _routing (explicit or disabled).
    Mutates *result* in place.
    """
    result["all_tenants"].append(tenant)

    # Collect tenant keys for schema validation
    if tenant not in result["tenant_keys"]:
        result["tenant_keys"][tenant] = set()
    result["tenant_keys"][tenant].update(overrides.keys())

    # v2.1.0 ADR-007: Extract _routing_profile reference
    rp_ref = overrides.get("_routing_profile")
    if rp_ref and isinstance(rp_ref, str):
        result["tenant_profile_refs"][tenant] = rp_ref.strip()

    # Severity dedup: default "enable", explicit "disable" to opt out
    raw_dedup = overrides.get("_severity_dedup", "enable")
    dedup_val = str(raw_dedup).strip().lower()
    if _is_disabled(dedup_val):
        result["dedup_configs"][tenant] = "disable"
    else:
        result["dedup_configs"][tenant] = "enable"

    # v1.11.0: Metadata extraction
    metadata = overrides.get("_metadata")
    if metadata and isinstance(metadata, dict):
        result["metadata_configs"][tenant] = _substitute_tenant(metadata, tenant)

    # Routing: "disable" string → skip routing
    routing = overrides.get("_routing")
    if isinstance(routing, str) and _is_disabled(routing):
        result["disabled_tenants"].add(tenant)
        return

    if routing and isinstance(routing, dict):
        result["explicit_routing"][tenant] = routing


def _parse_config_files(config_dir: str) -> dict:
    """Parse all YAML files in config_dir and extract raw data.

    Returns a dict with keys:
        all_tenants, defaults_keys, routing_defaults, enforced_routing,
        explicit_routing, disabled_tenants, dedup_configs, metadata_configs,
        tenant_keys, routing_profiles, domain_policies, tenant_profile_refs.

    Delegates to:
        _parse_platform_config() — platform-level keys from _ prefixed files
        _parse_tenant_overrides() — per-tenant overrides from tenant sections
    """
    result = {
        "all_tenants": [],
        "defaults_keys": set(),
        "optional_override_keys": set(),  # #1189: declared, platform-valueless
        "routing_defaults": {},
        "enforced_routing": None,
        "explicit_routing": {},
        "disabled_tenants": set(),
        "dedup_configs": {},
        "metadata_configs": {},
        "tenant_keys": {},
        "routing_profiles": {},      # v2.1.0 ADR-007: named routing configs
        "domain_policies": {},       # v2.1.0 ADR-007: domain compliance constraints
        "tenant_profile_refs": {},   # v2.1.0 ADR-007: tenant → profile name
        "policy_misplacements": [],  # ADR-007 --strict: domain_policies in wrong file
        "policy_file_errors": [],    # ADR-007 --strict: unparseable _domain_policy.yaml
    }

    if not os.path.isdir(config_dir):
        print(f"ERROR: config directory not found: {config_dir}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # #1339: this reader is FLAT while threshold-exporter walks the same
    # tree recursively (ADR-016/017). Routing for a hierarchical conf.d is
    # not implemented — but it used to fail SILENTLY ("No tenants found",
    # zero routes), which reads like "this config needs no routing" rather
    # than "this tool cannot see your tenants". Say which files are skipped.
    warn_nested(config_dir, tool="routing generator")

    files = sorted(f for f in os.listdir(config_dir)
                   if (f.endswith(".yaml") or f.endswith(".yml"))
                   and not f.startswith("."))

    for fname in files:
        path = os.path.join(config_dir, fname)
        # ⛔ `open()` is INSIDE the try. It was outside it for one round, and
        # an open-ended `except` around only `safe_load` reads exactly like
        # one that covers the read — measured: a *directory* named
        # `beta.yaml` (an interrupted `mkdir`, a bad merge) raised
        # `PermissionError` from this line, escaped every clause below, and
        # made `validate-config` exit **2** ("this tool broke, report it")
        # while its own `yaml_syntax` row said PASS. The scope of a guard is
        # part of the guard.
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            _drop_unusable_policy(
                fname, f"failed to parse: {e}",
                f"repair the YAML syntax in {fname}", result,
                warning=f"  WARN: skip unparseable {fname}: {e}")
            continue
        except UnicodeDecodeError as e:
            # An editor that saved this file in the local code page.
            # #1448 made this a first-class finding in validate-config;
            # the sibling reader on the generate-routes / explain-route
            # path used to let it escape as a traceback.
            _drop_unusable_policy(
                fname,
                f"not valid UTF-8 ({e.reason} at byte {e.start})",
                f"re-save {fname} as UTF-8", result)
            continue
        except Exception as e:  # noqa: BLE001
            # ⛔ Open-ended, and for the same reason `check_yaml_syntax`
            # is: this loop is reading ONE named file, so whatever comes
            # back it can say which one. Listing the types is a bet that
            # keeps losing — the first version of this branch listed the
            # two above, and a customer file nested ~500 levels deep
            # (roughly half `sys.getrecursionlimit()`:
            # 495 flow-style, 493 block-style on this host)
            # made `yaml.safe_load` raise `RecursionError` (not a
            # YAMLError), which escaped here and killed
            # `generate-routes --validate --strict` with a traceback and
            # zero bytes on stdout: #1448's original symptom, surviving
            # on the shared path the fix claimed to have closed.
            _drop_unusable_policy(
                fname,
                f"could not be read — {e.__class__.__name__}: "
                f"{' '.join(str(e).split())}",
                f"see the error above for what {fname} needs", result)
            continue

        # ⛔ `is None`, not `not data`, and the shape check comes FIRST.
        # `if not data: continue` sat above the isinstance check for one
        # round, so every FALSY non-mapping — `[]`, `0`, `false` — skipped
        # out before anything looked at its shape. Measured: a
        # `_domain_policy.yaml` whose whole body is `[]` left `--strict` at
        # exit 0 printing `OK: all configs valid` with zero policies
        # enforced, while `validate-config` called the same file "top level
        # must be a mapping" and exited 1. Two readers, two verdicts, one
        # file. An empty mapping (`{}`) still skips quietly below — that one
        # really is a document with nothing in it.
        if data is None:
            continue

        # #1447: a document that parses cleanly but is not a mapping (a bare
        # YAML list, a lone scalar) reaches `data.get(...)` below and raises
        # AttributeError, which took the whole run with it. It is a
        # wrong-shaped *input*, not a defect here, so it drops out the same
        # way an unparseable file does — through the same helper, so the two
        # cannot report differently.
        if not isinstance(data, dict):
            _drop_unusable_policy(
                fname,
                f"top level must be a mapping, got {type(data).__name__}",
                f"make {fname} a mapping — the keys this reader looks for "
                f"(tenants, defaults, domain_policies, _routing_*) all live "
                f"at its top level",
                result)
            continue

        if not data:
            continue

        _parse_platform_config(data, fname, result)

        if "tenants" not in data:
            continue

        # `is None`, not the get() default: `tenants:` with every entry
        # commented out parses to None, and the default only fires when the
        # key is absent — the `"tenants" not in data` check above already let
        # that through. #1447: this raised AttributeError and killed the run.
        # (An earlier revision wrote this as `data.get("tenants") or {}` and
        # the comment still said `or {}` after the code stopped doing it.)
        raw_tenants = data.get("tenants")
        if raw_tenants is None:
            # ⛔ Says so rather than passing silently. Commenting a tenant
            # out is a legitimate way to disable it, so this is not an
            # error and the exit code does not change — but "I disabled it"
            # and "I commented out one threshold and took the block with
            # it" produce byte-identical files, and the sibling branch
            # below already warns for a wrong-shaped `tenants:`. Before
            # this, only the null spelling was silent.
            print(f"  WARN: {fname}: 'tenants:' is declared but empty — no "
                  f"tenant is loaded from this file", file=sys.stderr)
            continue
        tenants = raw_tenants
        if not isinstance(tenants, dict):
            # ⚠️ THE ASYMMETRY IS THE POINT, and this reader is the lenient
            # side. Go declares this field
            # `map[string]map[string]ScheduledValue`
            # (`pkg/config/types.go:239`), so a sequence here is a
            # `yaml.TypeError`, `parsePartialConfig` returns ok=false
            # (`flat_scanner.go:69-80`) and the **entire file is dropped** —
            # taking `defaults:` with it, which this loop has already
            # harvested a few lines above. Same bytes: CI green here, empty
            # platform defaults in the exporter. That is verbatim the hazard
            # the `optional_overrides` branch of this same function warns
            # about, and this branch used to be a crash rather than a skip,
            # so the quiet version is new. Say it in the warning until the
            # two readers agree on a verdict.
            print(f"  WARN: {fname}: 'tenants' must be a mapping, got "
                  f"{type(tenants).__name__} — no tenant in this file is "
                  f"loaded here, and the Go exporter drops the WHOLE file "
                  f"on it (including its `defaults:`)", file=sys.stderr)
            continue
        for tenant, overrides in tenants.items():
            if not isinstance(overrides, dict):
                continue
            _parse_tenant_overrides(tenant, overrides, result)

    return result


def _merge_tenant_routing(parsed: dict, routing_defaults: dict) -> dict[str, dict]:
    """Merge routing defaults with explicit tenant routing configs.

    v2.1.0 ADR-007: Four-layer merge pipeline:
      1. _routing_defaults → global defaults
      2. routing_profiles[ref] → team/domain shared config (if _routing_profile set)
      3. tenant _routing → per-tenant overrides
      4. _routing_enforced → NOC immutable override (applied later in generate_routes)

    Returns routing_configs dict {tenant: merged_routing}.
    """
    profiles = parsed.get("routing_profiles", {})
    profile_refs = parsed.get("tenant_profile_refs", {})

    routing_configs = {}
    seen_tenants = set()
    for tenant in sorted(set(parsed["all_tenants"])):
        if tenant in parsed["disabled_tenants"] or tenant in seen_tenants:
            continue
        seen_tenants.add(tenant)

        # Layer 1: Start with routing defaults
        base = dict(routing_defaults) if routing_defaults else {}

        # Layer 2: Merge routing profile (if referenced)
        if tenant in profile_refs:
            profile_name = profile_refs[tenant]
            if profile_name in profiles:
                profile_cfg = profiles[profile_name]
                if isinstance(profile_cfg, dict):
                    for k, v in profile_cfg.items():
                        base[k] = v
            # Warning for unknown profile is emitted by _validate_profile_refs()

        # Layer 3: Merge explicit tenant routing overrides
        tenant_routing = parsed["explicit_routing"].get(tenant)

        # Only produce a routing config if there's something to route
        if tenant_routing or base:
            routing_configs[tenant] = merge_routing_with_defaults(
                base, tenant_routing, tenant)

    return routing_configs


def load_tenant_configs(
    config_dir: str,
    *,
    strict_policies: bool = False,
) -> tuple[dict[str, dict], dict[str, str], list[str], dict | None, dict[str, dict]]:
    """Load and parse all tenant YAML files from a config directory.

    Orchestrates the full configuration pipeline:
      1. Parse all .yaml/.yml files in config_dir (delegated to _parse_config_files())
      2. Merge _routing_defaults with tenant _routing overrides (delegated to _merge_tenant_routing())
      3. Validate tenant keys against defaults (checks for typos)
      4. Resolve routing profile references (ADR-007)
      5. Validate domain policies against resolved routes (ADR-007);
         with strict_policies=True violations are emitted as ERROR
         (blocking for the --strict CLI/CI path) instead of WARN.

    Configuration Hierarchy (4 layers):
      - Layer 0: _routing_defaults from _ prefixed files
      - Layer 1: routing_profile reference (_routing_profile key)
      - Layer 2: profile config resolved from _routing_profiles.yaml
      - Layer 3: explicit tenant _routing overrides

    Returns:
        (routing_configs, dedup_configs, schema_warnings, enforced_routing, metadata_configs):
        - routing_configs: {tenant_name: routing_config_dict} for tenants with _routing
        - dedup_configs: {tenant_name: "enable"|"disable"} for ALL tenants (default: "enable")
        - schema_warnings: list of validation warning strings
        - enforced_routing: dict or None — platform enforced routing config (v1.7.0+)
        - metadata_configs: {tenant_name: {runbook_url, owner, tier, ...}} (v1.11.0+)

    Note:
        All tenants appear in dedup_configs even if they have no _routing config.
    """
    parsed = _parse_config_files(config_dir)

    routing_configs = _merge_tenant_routing(
        parsed, parsed["routing_defaults"])

    # Schema validation: check tenant keys against defaults
    schema_warnings = []
    for tenant, keys in sorted(parsed["tenant_keys"].items()):
        schema_warnings.extend(
            validate_tenant_keys(tenant, keys, parsed["defaults_keys"],
                                 parsed["optional_override_keys"]))

    # v2.1.0 ADR-007: Validate profile references
    schema_warnings.extend(_validate_profile_refs(parsed))

    # v2.1.0 ADR-007: Validate domain policies against resolved routing
    if parsed["domain_policies"]:
        schema_warnings.extend(
            check_domain_policies(routing_configs, parsed["domain_policies"],
                                  strict=strict_policies))

    # ADR-007 --strict fail-open closures: an unparseable _domain_policy.yaml
    # or a domain_policies block in a wrongly named file makes every policy
    # silently vanish (validation stays green). Strict mode surfaces both as
    # blocking ERRORs; non-strict keeps the legacy stderr WARN prints only.
    if strict_policies:
        for err in parsed.get("policy_file_errors", []):
            # The cause and the remedy travel with the record (see
            # _drop_unusable_policy); this line only frames the consequence.
            schema_warnings.append(
                f"  {POLICY_ERROR_PREFIX} domain policy file could not be "
                f"used — every domain policy is silently dropped: {err}")
        for fname in parsed.get("policy_misplacements", []):
            schema_warnings.append(
                f"  {POLICY_ERROR_PREFIX} domain_policies block in "
                f"'{fname}' is ignored (only _domain_policy.yaml is "
                f"loaded), so those policies are not enforced — fix: move "
                f"the domain_policies block into _domain_policy.yaml")

    return (routing_configs, parsed["dedup_configs"], schema_warnings,
            parsed["enforced_routing"], parsed["metadata_configs"])
