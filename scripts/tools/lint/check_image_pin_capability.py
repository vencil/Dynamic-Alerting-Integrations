#!/usr/bin/env python3
"""check_image_pin_capability.py — a pinned da-tools image must actually CONTAIN the program the workload runs.

Why this exists
---------------
Two shipped workloads were pinned to `ghcr.io/vencil/da-tools:v2.9.0` — an
image built from a tag whose tree does not contain the program they invoke:

  1. `k8s/03-monitoring/cronjob-threshold-govern.yaml` runs the entrypoint
     subcommand `threshold-govern`. `tools/v2.9.0`'s `entrypoint.py` has no
     such key in `COMMAND_MAP` (the subcommand landed after the tag), so the
     weekly Job dies with `Unknown command` + exit 1 — silently, because the
     repo has NO job-failure alert at all (`kube_job_failed` /
     `kube_job_status_failed` appear nowhere in it) and the one alert that
     watches this CronJob, `ThresholdGovernanceStale`, keys on a series
     (`kube_cronjob_status_last_successful_time`) that a never-successful
     CronJob never produces.
  2. `helm/federation-reconciler` runs `/opt/da-tools/
     _federation_revocation_reconciler.py` directly. That file is only in
     HEAD's `build.sh` TOOL_FILES; `tools/v2.9.0`'s build.sh never copied it,
     so the container CrashLoops on a missing file.

Both are the SAME class of defect: a version pin that satisfies the deploy
plane (the image pulls, the manifest validates, kube-linter is happy) while
being incapable of the thing it was deployed to do. Nothing in the repo
noticed, because "does image X contain program Y" is a fact about a git TAG,
and no lint had ever crossed the manifest plane with the tag plane. This gate
does exactly that crossing, mechanically, on every PR.

What it checks
--------------
For every `ghcr.io/vencil/da-tools:vX.Y.Z` pin reachable from the scan
surface, resolve the workload's entry point and assert it exists in the
`tools/vX.Y.Z` tag's tree:

  entrypoint subcommand   container `args[0]` (e.g. `threshold-govern`)
                          -> must be a key of `COMMAND_MAP` in
                             `components/da-tools/app/entrypoint.py` AND the
                             script it maps to must be listed in `build.sh`
                             TOOL_FILES (registered-but-not-shipped is the
                             #1044 failure mode and is equally fatal).

  direct script path      a `command:` element of the form
                          `/opt/da-tools/<name>.py`
                          -> `<name>.py` must be a TOOL_FILES basename
                             (build.sh copies TOOL_FILES flat into the image's
                             /opt/da-tools, so the basename is the contract).

Scan surface
------------
  k8s/**/*.yaml             literal `image:` strings, full YAML parse
  helm/*/values*.yaml       image pins as a `repository:` + `tag:` PAIR
  helm/*/templates/**/*.yaml container `command:` / `args:`, parsed after
                            stripping Go-template actions. RECURSIVE on
                            purpose: the pre-commit `files:` regex matches
                            `templates/<subdir>/x.yaml` too, so a
                            non-recursive glob would give a hook that FIRES
                            on a file the scanner never opens — and prints OK.

The pin MUST be read by parsing YAML, never by scanning lines: a Helm values
pin is split across two lines (`repository:` / `tag:`) and so is invisible to
any line-oriented regex. That blind spot is exactly why `bump_docs.py` — which
does scan lines — has never seen the federation-reconciler pin at all.

Fail-closed
-----------
Four conditions are errors, never a silent pass:

  * the pinned `tools/vX.Y.Z` tag does not resolve locally (shallow clone) —
    the check cannot be performed, so it must not report success;
  * a da-tools workload's entry point cannot be resolved from its
    `command:` / `args:`;
  * `entrypoint.py` / `build.sh` cannot be read (or parses to nothing) at the
    pinned tag;
  * a chart pins a da-tools image but its templates yield ZERO containers.
    Charts are scanned as text-stripped YAML, and several perfectly ordinary
    shapes parse to nothing checkable — containers injected by a whole-line
    `{{- include "x.containers" . | nindent 8 }}`, or a `templates/` tree with
    no `.yaml` at all. Reporting "0 workloads checked, OK" for those would be
    the exact silent pass this gate exists to remove, so it is an error that
    names the chart and asks for a resolvable shape (or an explicit
    EXEMPTIONS entry) instead.

Scope (MVP): da-tools only
--------------------------
Deliberately covers ONLY `ghcr.io/vencil/da-tools`. Both known incidents are
da-tools, and da-tools is structurally the vulnerable one: it is a MULTI-TOOL
image whose contents are an explicit, hand-maintained whitelist (build.sh
TOOL_FILES + entrypoint.py COMMAND_MAP), so "the tag moved but the program
did not" is a routine outcome. The platform's other own-built components
(tenant-api / portal / recipe-preview / exporter) are single-purpose images
whose "entry point" is the binary they are built from — there is no per-tag
whitelist to fall out of sync with, and an equivalent check would have to
guess at flag-level compatibility, which produces false positives without
catching a real defect class. They are OUT OF SCOPE on purpose.

Extension trigger: a THIRD incident of this class on a non-da-tools image.
At that point generalise the resolver (image repo -> tag prefix -> capability
extractor) rather than bolting on a second special case.

Known non-covered surfaces (accepted, not oversights):
  * `.github/workflows/**` — `config-diff.yaml` runs a da-tools image too.
    Workflow pins fail LOUDLY (a red CI job), which is the opposite of the
    silent-failure property this gate exists for.
  * digest-pinned images (`image.digest` set) — a digest cannot be mapped
    back to a `tools/v*` tag; this is reported as an error, not skipped.
  * charts that mix a da-tools pin with other images — see
    `collect_helm_workloads`; reported as an error rather than guessed at.

Usage
-----
  python3 scripts/tools/lint/check_image_pin_capability.py

Exit codes:
  0 = every da-tools pin can run its workload (registered exemptions aside)
  1 = at least one incapable pin, or a stale EXEMPTIONS entry to delete
  2 = the check could not be RUN (tag not fetched / unresolvable workload /
      unreadable capability source)
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]

# The image whose capability is tag-dependent (see "Scope (MVP)" above).
DA_TOOLS_REPO = "ghcr.io/vencil/da-tools"
# build.sh copies TOOL_FILES FLAT into this directory (Dockerfile WORKDIR),
# so a `/opt/da-tools/<name>.py` invocation resolves against TOOL_FILES
# BASENAMES, not against the `ops/…` paths as written in build.sh.
IMAGE_TOOLS_DIR = "/opt/da-tools/"
# Image tag vX.Y.Z <-> git tag tools/vX.Y.Z (the da-tools release line).
TAG_PREFIX = "tools/"
CAPABILITY_SOURCES = {
    "entrypoint": "components/da-tools/app/entrypoint.py",
    "build_sh": "components/da-tools/app/build.sh",
}

# --- Central exemption registry --------------------------------------------
# (source, entry, image_tag) -> rationale. `source` is the repo-relative
# manifest path for a raw k8s workload, or the CHART DIRECTORY for a Helm
# workload (the pin lives in values.yaml, the invocation in templates/ — the
# chart is the only stable identity spanning both).
#
# The TAG is part of the key on purpose. An exemption is a statement about one
# specific image ("v2.9.0 cannot run this"), not a standing licence for the
# workload: with a (source, entry) key, re-pinning to ANOTHER tag that is
# equally incapable — say v2.8.0 — would inherit the exemption and pass the
# gate silently, which is precisely the failure mode this whole gate exists to
# remove. Keyed by tag, any movement of the pin forces re-registration, and a
# pin bumped to a CAPABLE tag turns the entry stale (below).
#
# Every entry MUST carry an explicit exit condition. An exemption that the
# pinned tag has since SATISFIED is reported as a violation (a stale entry is
# a lie about the deploy plane, and an un-self-cleaning registry only grows) —
# the fix is to delete the two lines.
EXEMPTIONS: dict[tuple[str, str, str], str] = {
    ("k8s/03-monitoring/cronjob-threshold-govern.yaml", "threshold-govern", "v2.9.0"):
        "#656 shipped threshold_govern.py AFTER tools/v2.9.0 was cut, and "
        "tools/v2.9.0 is still the newest da-tools tag, so no released image "
        "can run this CronJob. Left ENABLED rather than suspend:-ed because "
        "`suspend: true` asserts a deliberate pause, which this is not (it is "
        "broken and awaiting a release), and because un-suspending schedules "
        "an immediate catch-up run for the most recent missed slot. Note this "
        "outage has NO runtime detector: ThresholdGovernanceStale keys on "
        "kube_cronjob_status_last_successful_time, which a CronJob that has "
        "never succeeded never emits (deliberate cold-start safety, pinned by "
        "tests/rulepacks/platform-watchdog_test.yaml "
        "'governance-cold-start-absent-no-fire'), and the repo has no "
        "job-failure alert — THIS GATE IS THE ONLY SIGNAL. EXIT: bump the pin "
        "in the next tools/v* release; then delete this entry (the gate will "
        "demand it).",
    ("helm/federation-reconciler", "_federation_revocation_reconciler.py", "v2.9.0"):
        "#924 / ADR-028 added the reconciler to build.sh TOOL_FILES AFTER "
        "tools/v2.9.0 was cut, so the pinned image has no such file. Not "
        "disabled on purpose: FederationRevocationReconcileStale "
        "(configmap-rules-platform.yaml) is severity:critical / for:10m with "
        "an `or absent(...)` arm, so scaling the Deployment to zero pages "
        "on-call within 10 minutes. EXIT: bump image.tag (and Chart "
        "appVersion) in the next tools/v* release; then delete this entry "
        "(the gate will demand it).",
}

# --- Go-template stripping --------------------------------------------------
# A Helm template is not YAML until the actions are gone. Two-step, in order:
#   1. remove every `{{ … }}` span (DOTALL — `{{- /* … */ -}}` comment blocks
#      and `nindent` pipelines both wrap across lines) leaving a sentinel;
#   2. drop lines that are now nothing BUT sentinels (control flow such as
#      `{{- if … }}` / `{{- end }}` occupies a whole line and would otherwise
#      become a stray scalar inside a mapping and break the parse).
# Lines with a sentinel next to real content keep it as an opaque scalar. The
# result over-approximates: bodies of every `{{ if }}` branch are kept, which
# is the fail-closed direction for "find every container".
_TEMPLATE_ACTION_RE = re.compile(r"\{\{-?.*?-?\}\}", re.DOTALL)
_SENTINEL = "\x00tpl\x00"
_PLACEHOLDER = "__helm_template__"

# `repo:tag`, `repo@sha256:…`, or `repo` — the tag part may not contain "/"
# (that would make it a registry-port-and-path, not a tag).
_IMAGE_RE = re.compile(
    r"^(?P<repo>[^@]+?)(?::(?P<tag>[^:/@]+))?(?:@(?P<digest>[A-Za-z0-9]+:[0-9a-fA-F]+))?$"
)
_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def strip_helm_actions(text: str) -> str:
    """Return *text* with Go-template actions removed, parseable as YAML."""
    collapsed = _TEMPLATE_ACTION_RE.sub(_SENTINEL, text)
    kept: list[str] = []
    for line in collapsed.split("\n"):
        if line.strip() and not line.replace(_SENTINEL, "").strip():
            continue  # whole-line action (control flow / nindent include)
        kept.append(line.replace(_SENTINEL, _PLACEHOLDER))
    return "\n".join(kept)


# --- per-container image attribution (#1316) --------------------------------
# `strip_helm_actions` collapses every action to ONE opaque placeholder, so a
# parsed `image:` says "a template built this" and nothing more. That is what
# made chart-level attribution the only sound option, and why a chart holding a
# da-tools pin AND another repository's pin had to be an outright error.
#
# The indexed variant below keeps the same YAML shape but numbers the
# placeholders and hands back the action bodies, so a container's `image:` can
# be traced to the `.Values.<path>` it was assembled from — WITHOUT rendering
# (which would need helm on PATH and would silently skip wherever it is absent,
# the opposite of fail-closed; that trade-off is why the industry's
# render-then-lint tools do not help here).
#
# ⛔ Attribution only ever SUBTRACTS work: a container traced to a non-da-tools
# pin is skipped, and anything untraceable is treated exactly as before. A chart
# is still an error unless EVERY non-da-tools pin has a container claiming it —
# an unclaimed foreign pin means something in the chart runs an image this gate
# cannot see, which is the original ambiguity and stays fatal.
_INDEXED_PLACEHOLDER_RE = re.compile(r"__helm_action_(\d+)__")
_SENTINEL_IDX_RE = re.compile("\x00tpl(\\d+)\x00")
_VALUES_REF_RE = re.compile(r"\.Values\.([A-Za-z0-9_.]+)")


def strip_helm_actions_indexed(text: str) -> tuple[str, list[str]]:
    """`strip_helm_actions`, but each action keeps an identity.

    Returns the stripped text (with `__helm_action_<i>__` placeholders) and the
    action bodies, index-aligned. Same whole-line-action dropping rule, so the
    document parses to the same structure.
    """
    bodies: list[str] = []

    def _capture(match: re.Match) -> str:
        bodies.append(match.group(0))
        return f"\x00tpl{len(bodies) - 1}\x00"

    collapsed = _TEMPLATE_ACTION_RE.sub(_capture, text)
    kept: list[str] = []
    for line in collapsed.split("\n"):
        if line.strip() and not _SENTINEL_IDX_RE.sub("", line).strip():
            continue  # whole-line action (control flow / nindent include)
        kept.append(_SENTINEL_IDX_RE.sub(r"__helm_action_\1__", line))
    return "\n".join(kept), bodies


def image_values_paths(image_value, bodies: list[str]) -> set[str]:
    """The dotted `.Values` paths a container's `image:` expression reads."""
    if not isinstance(image_value, str):
        return set()
    paths: set[str] = set()
    for index in _INDEXED_PLACEHOLDER_RE.findall(image_value):
        position = int(index)
        if position < len(bodies):
            paths.update(_VALUES_REF_RE.findall(bodies[position]))
    return paths


def attributed_pin(image_paths: set[str], pin_trails: list[str]) -> str | None:
    """The pin trail a container's image expression belongs to, if exactly one.

    ⛔ Callers must pass EVERY pin in the chart, da-tools included, not just the
    foreign ones. Matching against the foreign set alone makes an expression
    that reads BOTH — say the da-tools repository with another pin's tag — look
    like a clean foreign hit, and the container this gate exists to check would
    be skipped. That is fail-OPEN, and it is the mutation that survived the
    first version of this function.

    Ambiguity stays unattributed on purpose: an expression touching two pins is
    exactly the case this mechanism refuses to guess at, and an unattributed
    container is handled as it was before attribution existed.
    """
    hits = {
        trail for trail in pin_trails
        for path in image_paths
        if path == trail or path.startswith(f"{trail}.")
    }
    return hits.pop() if len(hits) == 1 else None


# --- capability extraction (text in, facts out) ------------------------------
# Deliberately text-based, not path-based: the inputs come from `git show
# <tag>:<path>` and must never touch the working tree. Kept behaviourally
# identical to scripts/tools/lint/_lint_helpers.py's file-based parsers —
# tests/lint/test_check_image_pin_capability.py pins that equivalence on HEAD
# so the two cannot drift.
_COMMAND_MAP_ENTRY_RE = re.compile(r'"([a-z][a-z0-9-]+)":\s*"([^"]+)"')


def parse_command_map_text(text: str) -> dict[str, str]:
    """Parse `COMMAND_MAP` (subcommand -> script filename) from entrypoint.py source."""
    commands: dict[str, str] = {}
    in_map = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("COMMAND_MAP"):
            in_map = True
            continue
        if in_map:
            if stripped == "}":
                break
            match = _COMMAND_MAP_ENTRY_RE.match(stripped)
            if match:
                commands[match.group(1)] = match.group(2)
    return commands


def parse_tool_files_text(text: str) -> set[str]:
    """Parse `TOOL_FILES` from build.sh source, returning BASENAMES.

    Basenames, not the `ops/…` paths as written: build.sh copies the array
    flat into the image, so the basename is what `/opt/da-tools/<x>` resolves.
    """
    tools: set[str] = set()
    in_block = False
    for line in text.split("\n"):
        stripped = line.strip()
        if "TOOL_FILES=(" in stripped:
            in_block = True
            continue
        if in_block:
            if stripped == ")":
                break
            if not stripped or stripped.startswith("#"):
                continue
            name = stripped.strip("\"'(),").strip()
            if name:
                tools.add(os.path.basename(name))
    return tools


# --- git plumbing -----------------------------------------------------------
def _git(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    return result.returncode, result.stdout


def tag_exists(git_tag: str) -> bool:
    code, _ = _git(["rev-parse", "--verify", "--quiet", f"refs/tags/{git_tag}"])
    return code == 0


def show_blob(git_tag: str, path: str) -> str | None:
    code, out = _git(["show", f"{git_tag}:{path}"])
    return out if code == 0 else None


class CapabilityError(RuntimeError):
    """The pinned tag's capability set could not be determined."""


_capability_cache: dict[str, tuple[dict[str, str], set[str]]] = {}


def capabilities_for_tag(git_tag: str) -> tuple[dict[str, str], set[str]]:
    """Return (COMMAND_MAP, TOOL_FILES basenames) as of *git_tag*."""
    if git_tag in _capability_cache:
        return _capability_cache[git_tag]
    if not tag_exists(git_tag):
        raise CapabilityError(
            f"git tag '{git_tag}' does not resolve in this clone, so the image "
            f"pinned to it cannot be inspected.\n"
            f"    CI: the `lint` job already checks out with fetch-depth: 0 — "
            f"if this fires there, the tag genuinely does not exist upstream "
            f"(typo in the pin?).\n"
            f"    Locally: run `git fetch --tags`."
        )
    entrypoint_src = show_blob(git_tag, CAPABILITY_SOURCES["entrypoint"])
    build_src = show_blob(git_tag, CAPABILITY_SOURCES["build_sh"])
    if entrypoint_src is None or build_src is None:
        missing = [p for p, s in ((CAPABILITY_SOURCES["entrypoint"], entrypoint_src),
                                  (CAPABILITY_SOURCES["build_sh"], build_src)) if s is None]
        raise CapabilityError(
            f"tag '{git_tag}' has no {' / '.join(missing)} — cannot determine "
            f"what that image contains."
        )
    command_map = parse_command_map_text(entrypoint_src)
    tool_files = parse_tool_files_text(build_src)
    if not command_map or not tool_files:
        raise CapabilityError(
            f"parsed 0 COMMAND_MAP entries and/or 0 TOOL_FILES at tag "
            f"'{git_tag}' — the parser is out of step with that tag's source "
            f"layout; refusing to report a result from an empty capability set."
        )
    _capability_cache[git_tag] = (command_map, tool_files)
    return command_map, tool_files


# --- manifest walking -------------------------------------------------------
def parse_image_ref(image: str) -> tuple[str, str | None, str | None]:
    """Split an image reference into (repository, tag, digest)."""
    match = _IMAGE_RE.match(image.strip())
    if not match:
        return image.strip(), None, None
    return match.group("repo"), match.group("tag"), match.group("digest")


def iter_containers(node):
    """Yield every container mapping reachable from *node* (any workload kind)."""
    if isinstance(node, dict):
        for key in ("containers", "initContainers"):
            value = node.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        yield item
        for value in node.values():
            yield from iter_containers(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_containers(item)


def resolve_entry(container: dict) -> tuple[str, str] | None:
    """Return (kind, entry) for a container, or None if it cannot be resolved.

    kind is "script" (a `/opt/da-tools/<name>.py` in `command:`) or
    "subcommand" (`args[0]`, the image ENTRYPOINT's dispatch name).
    `command:` wins: an explicit command REPLACES the image entrypoint, so a
    container with both is not running a subcommand at all.
    """
    command = container.get("command")
    if isinstance(command, list):
        for element in command:
            if isinstance(element, str) and element.startswith(IMAGE_TOOLS_DIR):
                name = element[len(IMAGE_TOOLS_DIR):]
                if name.endswith(".py"):
                    return "script", name
        return None
    args = container.get("args")
    if isinstance(args, list) and args:
        first = args[0]
        if isinstance(first, str) and first and not first.startswith("-"):
            return "subcommand", first
    return None


def find_image_pins(node, trail: str = "") -> list[tuple[str, dict]]:
    """Yield (dotted-path, mapping) for every `repository:` pin in *node*.

    A missing `tag:` key does NOT disqualify a mapping: `repository:` with no
    `tag:` is a common chart idiom (the template writes
    `{{ .Values.image.tag | default .Chart.AppVersion }}`), and treating it as
    "not a pin" is a silent pass — the caller reports the missing tag as an
    error instead.
    """
    found: list[tuple[str, dict]] = []
    if isinstance(node, dict):
        if isinstance(node.get("repository"), str):
            found.append((trail or "<root>", node))
        for key, value in node.items():
            found.extend(find_image_pins(value, f"{trail}.{key}" if trail else str(key)))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(find_image_pins(item, f"{trail}[{index}]"))
    return found


class Workload:
    """One da-tools container plus the tag its image is pinned to."""

    def __init__(self, source: str, where: str, image_tag: str, kind: str, entry: str):
        self.source = source          # EXEMPTIONS key half: manifest path or chart dir
        self.where = where            # human-readable location for the message
        self.image_tag = image_tag    # "v2.9.0"
        self.kind = kind              # "subcommand" | "script"
        self.entry = entry            # "threshold-govern" | "foo.py"

    @property
    def git_tag(self) -> str:
        return f"{TAG_PREFIX}{self.image_tag}"

    @property
    def key(self) -> tuple[str, str, str]:
        # The TAG is part of the identity: an exemption licenses one image,
        # not the workload forever (see EXEMPTIONS).
        return (self.source, self.entry, self.image_tag)


def _yaml_files(root: Path, pattern: str = "*") -> list[Path]:
    """Both YAML extensions — the pre-commit `files:` regex accepts `.yml` too,
    so globbing only `.yaml` would leave a hook that fires on a file the
    scanner never opens."""
    found = {p for ext in ("yaml", "yml") for p in root.glob(f"{pattern}.{ext}")}
    return sorted(p for p in found if p.is_file())


def _k8s_files() -> list[Path]:
    return _yaml_files(REPO_ROOT / "k8s", "**/*")


def _chart_dirs() -> list[Path]:
    return sorted({p.parent for ext in ("yaml", "yml")
                   for p in REPO_ROOT.glob(f"helm/*/Chart.{ext}")})


def collect_k8s_workloads(errors: list[str]) -> list[Workload]:
    """da-tools containers in raw k8s manifests (literal image strings)."""
    workloads: list[Workload] = []
    for path in _k8s_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except yaml.YAMLError as exc:
            errors.append(f"{rel}: not parseable as YAML, so any image pin in it "
                          f"is invisible to this gate ({exc.__class__.__name__})")
            continue
        for doc in docs:
            for container in iter_containers(doc):
                image = container.get("image")
                if not isinstance(image, str):
                    continue
                repo, tag, digest = parse_image_ref(image)
                if repo != DA_TOOLS_REPO:
                    continue
                name = container.get("name", "<unnamed>")
                if digest:
                    errors.append(f"{rel} (container {name}): image is digest-pinned "
                                  f"({digest}); a digest cannot be mapped to a "
                                  f"{TAG_PREFIX}v* tag, so capability is unverifiable")
                    continue
                if not tag or not _TAG_RE.match(tag):
                    errors.append(f"{rel} (container {name}): da-tools image tag "
                                  f"{tag!r} is not a vX.Y.Z release tag; this gate "
                                  f"cannot map it to a {TAG_PREFIX}v* git tag")
                    continue
                resolved = resolve_entry(container)
                if resolved is None:
                    errors.append(f"{rel} (container {name}): cannot tell what this "
                                  f"da-tools container runs — no "
                                  f"{IMAGE_TOOLS_DIR}*.py in `command:` and no "
                                  f"subcommand as `args[0]`")
                    continue
                kind, entry = resolved
                workloads.append(Workload(rel, f"{rel} (container {name})", tag, kind, entry))
    return workloads


def collect_helm_workloads(errors: list[str]) -> list[Workload]:
    """da-tools containers in Helm charts (pins from values*.yaml, invocation from templates/).

    Association is CHART-level, deliberately. A chart's pin lives in
    values.yaml while the container's `image:` is a Go-template expression, so
    the two cannot be re-associated per-container without rendering the chart
    (which would need helm on PATH and would silently skip wherever it is
    absent — the opposite of fail-closed). Consequences, all fail-closed:

      * every `values*.yaml` in the chart is read, not just the default —
        an override that re-pins `image.tag` is a real deployment and is
        checked against the same containers;
      * a chart mixing a da-tools pin with another repository's pin is an
        ERROR, because attribution genuinely is ambiguous there;
      * a container in a da-tools-pinned chart whose entry point cannot be
        resolved is an ERROR, not a skip;
      * a da-tools-pinned chart whose templates yield ZERO containers is an
        ERROR. Several ordinary chart shapes parse to nothing — containers
        injected wholesale by `{{- include "x.containers" . | nindent 8 }}`
        (a whole-line action, dropped by strip_helm_actions), or a
        `templates/` tree with no `.yaml` — and "found nothing, therefore
        fine" on a chart we KNOW ships a da-tools image is the silent pass
        this gate exists to remove.
    """
    workloads: list[Workload] = []
    for chart in _chart_dirs():
        chart_rel = chart.relative_to(REPO_ROOT).as_posix()
        values_files = _yaml_files(chart, "values*")
        if not values_files:
            continue
        da_tools_pins: list[tuple[str, dict]] = []
        other_repos: set[str] = set()
        other_trails: dict[str, str] = {}   # values trail -> repository (#1316)
        da_tools_trails: list[str] = []     # so a mixed expression stays unattributed
        unreadable = False
        for values in values_files:
            values_rel = values.relative_to(REPO_ROOT).as_posix()
            try:
                values_doc = yaml.safe_load(values.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                errors.append(f"{values_rel}: not parseable as YAML "
                              f"({exc.__class__.__name__}), so an image pin in it "
                              f"is invisible to this gate")
                unreadable = True
                continue
            for trail, pin in find_image_pins(values_doc):
                if pin.get("repository") == DA_TOOLS_REPO:
                    da_tools_pins.append((f"{values_rel}:{trail}", pin))
                    da_tools_trails.append(trail)
                elif isinstance(pin.get("repository"), str):
                    other_repos.add(pin["repository"])
                    other_trails[trail] = pin["repository"]
        if unreadable or not da_tools_pins:
            continue
        tags: list[str] = []
        broken = False
        for trail, pin in da_tools_pins:
            if pin.get("digest"):
                errors.append(f"{trail}.digest is set; a digest cannot be mapped to "
                              f"a {TAG_PREFIX}v* tag, so capability is unverifiable")
                broken = True
                continue
            if "tag" not in pin:
                errors.append(
                    f"{trail} pins the da-tools repository with NO `tag:` key, so "
                    f"the running image is whatever the template defaults to "
                    f"(typically `.Chart.AppVersion`). This gate will not guess "
                    f"at that: add an explicit `tag:` to values.yaml so the pin "
                    f"is a fact rather than a template default")
                broken = True
                continue
            tag = pin.get("tag")
            if not isinstance(tag, str) or not _TAG_RE.match(tag):
                errors.append(f"{trail}.tag is {tag!r}, not a vX.Y.Z release tag")
                broken = True
                continue
            if tag not in tags:
                tags.append(tag)
        if broken:
            continue
        containers_seen = 0
        chart_workloads: list[Workload] = []
        chart_errors: list[str] = []
        claimed_trails: set[str] = set()   # non-da-tools pins a container claims
        # RECURSIVE: the pre-commit `files:` regex matches
        # `templates/<subdir>/x.yaml`, so a non-recursive glob would leave a
        # hook that fires on a file the scanner never opens — and prints OK.
        for template in _yaml_files(chart / "templates", "**/*"):
            template_rel = template.relative_to(REPO_ROOT).as_posix()
            stripped, actions = strip_helm_actions_indexed(
                template.read_text(encoding="utf-8"))
            try:
                docs = list(yaml.safe_load_all(stripped))
            except yaml.YAMLError as exc:
                chart_errors.append(f"{template_rel}: not parseable as YAML even after "
                                    f"stripping template actions "
                                    f"({exc.__class__.__name__}) — a da-tools container "
                                    f"in it would be invisible to this gate")
                continue
            for doc in docs:
                for container in iter_containers(doc):
                    name = container.get("name", "<unnamed>")
                    # #1316: a container whose image expression reads a
                    # NON-da-tools pin is not a da-tools container, so its entry
                    # point is none of this gate's business. Untraceable ones
                    # fall through to the original treatment.
                    owner = attributed_pin(
                        image_values_paths(container.get("image"), actions),
                        list(other_trails) + da_tools_trails)
                    # ⛔ `not in da_tools_trails` is the second half of the veto,
                    # and omitting it was a fail-OPEN this gate did not have
                    # before. ONE trail can be both: every `values*.yaml` in the
                    # chart is read, so a `values-prod.yaml` re-pointing
                    # `image.repository` at a mirror or a fork puts `image` in
                    # BOTH sets. Skipping on the foreign membership alone then
                    # excused the very container the gate exists to check, and
                    # the chart went green where it used to raise the
                    # mixed-image error. (Charts with a single container are
                    # caught by the containers_seen==0 arm below — but only
                    # those, and with a message pointing at the wrong cause.)
                    if (owner is not None and owner in other_trails
                            and owner not in da_tools_trails):
                        claimed_trails.add(owner)
                        continue
                    containers_seen += 1
                    resolved = resolve_entry(container)
                    if resolved is None:
                        chart_errors.append(
                            f"{template_rel} (container {name}): chart pins the "
                            f"da-tools image but this container's entry point "
                            f"cannot be resolved (no {IMAGE_TOOLS_DIR}*.py in "
                            f"`command:`, no subcommand as `args[0]`)")
                        continue
                    kind, entry = resolved
                    for tag in tags:
                        chart_workloads.append(Workload(
                            chart_rel, f"{template_rel} (container {name}) @ {tag}",
                            tag, kind, entry))
        unclaimed = sorted(repo for trail, repo in other_trails.items()
                           if trail not in claimed_trails)
        if unclaimed:
            # The original chart-level refusal, now narrowed to the case that is
            # still genuinely ambiguous: a foreign pin no container claims means
            # something here runs an image this gate cannot see.
            errors.append(f"{chart_rel}: pins da-tools AND {unclaimed} with no "
                          f"container whose `image:` reads that pin; chart-level "
                          f"attribution is ambiguous for a mixed-image chart — "
                          f"extend collect_helm_workloads() before landing it")
            continue
        errors.extend(chart_errors)
        workloads.extend(chart_workloads)
        if containers_seen == 0:
            errors.append(
                f"{chart_rel}: pins the da-tools image ({', '.join(tags)}) but "
                f"NO container could be parsed out of {chart_rel}/templates — "
                f"so nothing was verified. Usual causes: the containers are "
                f"injected by a whole-line `{{{{- include \"…\" . | nindent N }}}}` "
                f"(stripped as control flow), or templates/ holds no .yaml at "
                f"all. Fix by writing the container list literally in a "
                f"template, or extend collect_helm_workloads() to resolve the "
                f"shape — an unparseable chart must not read as a pass. "
                f"(EXEMPTIONS cannot cover this: it keys on a resolved "
                f"workload, and no workload was resolved.)")
    return workloads


def evaluate(workload: Workload) -> str | None:
    """Return a defect description if the pinned tag cannot run *workload*, else None."""
    command_map, tool_files = capabilities_for_tag(workload.git_tag)
    if workload.kind == "subcommand":
        script = command_map.get(workload.entry)
        if script is None:
            return (f"entrypoint subcommand '{workload.entry}' is not in "
                    f"COMMAND_MAP at {workload.git_tag} (the container exits 1 "
                    f"with `Unknown command`)")
        if script not in tool_files:
            return (f"entrypoint subcommand '{workload.entry}' maps to {script}, "
                    f"which is NOT in build.sh TOOL_FILES at {workload.git_tag} "
                    f"(registered but never copied into the image)")
        return None
    if workload.entry not in tool_files:
        return (f"{IMAGE_TOOLS_DIR}{workload.entry} is not in build.sh TOOL_FILES "
                f"at {workload.git_tag} (the container starts and immediately "
                f"fails on a missing file)")
    return None


def run_check(workloads: list[Workload], exemptions: dict[tuple[str, str, str], str]):
    """Classify *workloads*. Returns (violations, exempted, stale_exemptions)."""
    violations: list[str] = []
    exempted: list[str] = []
    satisfied_keys: set[tuple[str, str, str]] = set()
    for workload in workloads:
        defect = evaluate(workload)
        if defect is None:
            if workload.key in exemptions:
                satisfied_keys.add(workload.key)
            continue
        if workload.key in exemptions:
            exempted.append(f"{workload.where}: {defect}\n      registered: "
                            f"{exemptions[workload.key]}")
            continue
        violations.append(f"{workload.where}: {defect}")
    stale = [f"{source} / {entry} @ {tag}: the pinned image now PROVIDES this "
             f"entry point — delete the EXEMPTIONS entry in "
             f"scripts/tools/lint/check_image_pin_capability.py"
             for source, entry, tag in sorted(satisfied_keys)]
    return violations, exempted, stale


def main() -> int:
    # argparse with no flags — enforces the repo-wide CLI contract
    # (test_help_exits_zero / test_invalid_args_exits_nonzero): `--help`
    # exits 0; unknown flags exit 2 (argparse default).
    # CJK in --help would raise UnicodeEncodeError on a legacy Windows console
    # (cp950/cp936), killing the tool before argparse ever prints.
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "斷言每個 pin 住的 da-tools image 真的含有該 workload 要跑的程式"
            "（entrypoint 子命令或 /opt/da-tools 下的 script）。",
            "Assert that every pinned da-tools image actually contains "
            "the program the workload pinned to it runs.",
        ),
    )
    parser.parse_args()

    errors: list[str] = []
    workloads = collect_k8s_workloads(errors) + collect_helm_workloads(errors)

    # Collection errors first: a pin we could not even resolve makes any
    # verdict about the rest incomplete, and reporting it is more actionable
    # than whatever the tag lookup would have said.
    if errors:
        print(f"ERROR: {len(errors)} da-tools pin(s) could not be checked "
              f"(fail-closed — this is NOT a pass):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        violations, exempted, stale = run_check(workloads, EXEMPTIONS)
    except CapabilityError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    for note in exempted:
        print(f"  EXEMPT {note}")

    if violations or stale:
        print(f"FAIL: {len(violations) + len(stale)} da-tools image pin problem(s):")
        for violation in violations:
            print(f"  - {violation}")
        for note in stale:
            print(f"  - STALE EXEMPTION {note}")
        if violations:
            print(
                "\nFix: bump the pin to a tools/v* tag whose tree contains the "
                "entry point, or — if no released tag does yet — register the "
                "workload in EXEMPTIONS (scripts/tools/lint/"
                "check_image_pin_capability.py) with a rationale AND an exit "
                "condition. The exemption key includes the TAG, so a pin that "
                "moves must be re-registered rather than inheriting the old "
                "licence.\nDo NOT 'fix' it by suspending the CronJob or scaling "
                "the Deployment to zero. Those are not equivalent, and neither "
                "is a fix: scaling federation-reconciler to zero PAGES on-call "
                "(FederationRevocationReconcileStale is severity:critical / "
                "for:10m with an `or absent(...)` arm), while suspending "
                "threshold-govern is silent — ThresholdGovernanceStale keys on "
                "a last-success series that a never-successful CronJob never "
                "emits, so there is no runtime detector to lose, and `suspend: "
                "true` would additionally assert a deliberate pause that is not "
                "what happened. In both cases the only real fix is a release "
                "that ships the entry point."
            )
        return EXIT_VIOLATION

    print(f"OK: {len(workloads)} da-tools workload(s) checked; every pinned image "
          f"contains the entry point it is asked to run "
          f"({len(exempted)} registered exemption(s)).")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
