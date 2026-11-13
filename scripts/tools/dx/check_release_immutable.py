#!/usr/bin/env python3
"""check_release_immutable.py — a released version is never published twice with different content (#3848).

`release.yaml` publishes by moving a tag (`docker buildx imagetools create`)
and by `helm push`. Both overwrite: a re-run, or a git tag deleted and pushed
again on another commit, puts different bytes under a version customers have
already pulled, and nothing said so. This asks the registry, before anything
is published, whether the version is already there, and what it is.

    image --ref <registry>/<repo>:<tag> --revision <commit> [--candidate-digest <digest>]
        Absent → publish. Present → read `org.opencontainers.image.revision`
        from the image config (an index: from every platform manifest, the
        attestation entries — platform os `unknown` — left out; they must all
        agree). Equal to <commit> → do not publish (the tag stays on the digest
        it has); different, or no label → violation: an already-published
        version cannot be re-tagged.

    chart --tgz <packaged.tgz> --registry oci://<registry>/<path>
        The chart's name and version are read from the archive itself; the tag
        is the version with `+` written `_` (helm's OCI rule). Absent →
        publish. Present → the published archive (manifest → the
        `application/vnd.cncf.helm.chart.content.v1.tar+gzip` layer → blob) is
        compared file by file with <packaged.tgz>: every Chart.yaml as YAML
        (helm re-serialises it, so formatting and comments are not content),
        every other file byte for byte. Same → do not publish (a re-run is
        idempotent); different → violation, naming the files.

The decision is printed and, when $GITHUB_OUTPUT is set, written there as
`publish=true|false` (image also `digest=`: the candidate's when publishing,
else the digest the tag already names — what a later step must describe).

"Absent" is only the registry's own 404 MANIFEST_UNKNOWN / NAME_UNKNOWN. A
401 / 403 is not: anonymously a registry answers a repository it does not
have exactly as it answers a private one, so a repository's first publish is
told from "cannot see it" only with credentials — $REGISTRY_USERNAME and
$REGISTRY_PASSWORD (both or neither), sent only after the anonymous attempt
is refused (see _lib_registry). With GITHUB_TOKEN, ghcr.io answers a
repository it does not have with 404 MANIFEST_UNKNOWN (measured 2026-11-13,
Actions run 43448765258), so a first publish is recognised.

release.yaml does not call this to gate anything: release_publish.py runs the
same decision inside the publish. Run alone, it answers "may this be
published?" without publishing (the playbook's manual-remedy check).

Exit codes
    0  decided (publish=true or publish=false)
    1  the version is published with other content — ship a new version
    2  could not decide: no answer from the registry (network, 5xx, 429,
       401/403, a reply that is not what was asked for, bytes that do not
       match their digest), or unreadable input. Never a silent publish.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tarfile
import zlib
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK,
    EXIT_VIOLATION,
    EXIT_CALLER_ERROR,
)
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402
import _lib_registry as registry  # noqa: E402

REVISION_LABEL = "org.opencontainers.image.revision"
CHART_LAYER = "application/vnd.cncf.helm.chart.content.v1.tar+gzip"
INDEX_TYPES = frozenset({
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
})
IMAGE_TYPES = frozenset({
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
})
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_CHART_BYTES = 50 * 1024 * 1024
#: Uncompressed size a chart archive may reach before it is refused as a bomb.
#: Enforced on the gunzip stream BEFORE tarfile parses anything, so tar
#: metadata (a PAX or GNU long-name header is read whole into memory) counts
#: too. A real chart is well under 1 MiB.
MAX_UNPACKED_BYTES = 64 * 1024 * 1024
#: Members a chart archive may hold before it is refused (a real chart has tens).
MAX_MEMBERS = 10_000
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

#: The HTTP seam (_lib_registry.Transport); None → the library's urllib one.
TRANSPORT = None


class CallerError(Exception):
    """Input this check cannot read — rc 2, never a decision."""


class Violation(Exception):
    """The version is published with other content — rc 1."""


class Decision(NamedTuple):
    publish: bool
    message: str
    digest: str = ""        # image only


# ── image ───────────────────────────────────────────────────────────────────


def _platform(entry: dict) -> str:
    p = entry.get("platform") if isinstance(entry.get("platform"), dict) else {}
    return "/".join(str(p[k]) for k in ("os", "architecture", "variant") if p.get(k)) or "<no platform>"


def _image_revision(repo: registry.Repository, manifest: registry.Manifest, where: str) -> Optional[str]:
    """The revision label of one image manifest's config; None when absent."""
    if manifest.media_type not in IMAGE_TYPES:
        raise registry.NoAnswer(f"{where}: {manifest.media_type} where an image manifest belongs")
    config = manifest.document.get("config")
    if not isinstance(config, dict) or not isinstance(config.get("digest"), str):
        raise registry.NoAnswer(f"{where}: manifest has no config descriptor")
    try:
        doc = json.loads(repo.blob(config["digest"], size=config.get("size"),
                                   max_bytes=MAX_CONFIG_BYTES))
    except (ValueError, RecursionError) as exc:
        raise registry.NoAnswer(f"{where}: image config is not JSON: {exc}") from exc
    inner = doc.get("config") if isinstance(doc, dict) else "<not an object>"
    if inner is not None and not isinstance(inner, dict):
        raise registry.NoAnswer(f"{where}: image config has no `config` object")
    labels = (inner or {}).get("Labels")
    if labels is not None and not isinstance(labels, dict):
        raise registry.NoAnswer(f"{where}: image config Labels is not a map")
    value = (labels or {}).get(REVISION_LABEL)
    return value if isinstance(value, str) and value else None


def judge_image(repo: registry.Repository, reference: str, revision: str,
                candidate: str = "") -> Decision:
    where = f"{repo.host}/{repo.repository}:{reference}"
    found = repo.manifest(reference)
    if found is None:
        return Decision(True, f"{where} is not published yet — publish", candidate)
    revisions: Dict[str, Optional[str]] = {}
    if found.media_type in INDEX_TYPES:
        entries = found.document.get("manifests")
        if not isinstance(entries, list):
            raise registry.NoAnswer(f"{where}: image index has no `manifests` list")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("digest"), str):
                raise registry.NoAnswer(f"{where}: image index entry without a digest")
            platform = entry.get("platform")
            if isinstance(platform, dict) and platform.get("os") == "unknown":
                continue                    # an attestation (provenance / SBOM), not an image
            child = repo.manifest(entry["digest"])
            if child is None:
                raise registry.NoAnswer(f"{where}: the index names {entry['digest']}, "
                                        f"which the registry does not have")
            label = f"{_platform(entry)} ({entry['digest']})"
            revisions[label] = _image_revision(repo, child, f"{where} {label}")
        if not revisions:
            raise registry.NoAnswer(f"{where}: image index lists no platform manifest")
    else:
        revisions[found.digest] = _image_revision(repo, found, where)
    missing = sorted(p for p, r in revisions.items() if r is None)
    if missing:
        raise Violation(
            f"{where} is already published, and no {REVISION_LABEL} label is on "
            f"{', '.join(missing)}, so nothing shows it was built from {revision}. "
            f"An already-published version cannot be re-tagged — ship a new version.")
    seen = sorted(set(revisions.values()))
    if len(seen) > 1:
        raise Violation(
            f"{where} is already published and its platforms disagree on {REVISION_LABEL}: "
            + "; ".join(f"{p}={r}" for p, r in sorted(revisions.items()))
            + ". An already-published version cannot be re-tagged — ship a new version.")
    if seen[0] != revision:
        raise Violation(
            f"{where} is already published from {seen[0]}, not {revision}. "
            f"An already-published version cannot be re-tagged — ship a new version.")
    return Decision(False, f"{where} is already published from {revision} "
                           f"({found.digest}) — not re-published, the tag stays", found.digest)


# ── chart ───────────────────────────────────────────────────────────────────


def _gunzip(data: bytes) -> bytes:
    """The tar stream inside a .tgz, never more than MAX_UNPACKED_BYTES."""
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = inflater.decompress(data, MAX_UNPACKED_BYTES + 1)
    if len(out) > MAX_UNPACKED_BYTES:
        raise ValueError(f"unpacks to more than {MAX_UNPACKED_BYTES} bytes")
    if not inflater.eof or inflater.unused_data:
        raise ValueError("not one complete gzip stream")
    return out


def chart_files(data: bytes) -> Dict[str, bytes]:
    """path → content of every member of a chart archive (directories left
    out; a link is its target, marked). Read in memory, never extracted; the
    gzip layer is inflated under a cap first, so no header can make tarfile
    allocate more than that."""
    files: Dict[str, bytes] = {}
    try:
        tar_bytes = _gunzip(data)
    except zlib.error as exc:
        raise ValueError(f"not gzip: {exc}") from exc
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
        for count, member in enumerate(tar, 1):
            if count > MAX_MEMBERS:
                raise ValueError(f"holds more than {MAX_MEMBERS} members")
            if member.isdir():
                continue
            if member.name in files:
                raise ValueError(f"the archive holds {member.name} twice")
            if member.isfile():
                handle = tar.extractfile(member)
                files[member.name] = handle.read() if handle else b""
            else:
                files[member.name] = f"<{member.type!r} -> {member.linkname}>".encode()
    return files


def _chart_yaml(files: Dict[str, bytes]) -> dict:
    import yaml
    tops = {name.split("/", 1)[0] for name in files}
    if len(tops) != 1:
        raise ValueError(f"not one top-level directory: {sorted(tops)}")
    top = tops.pop()
    raw = files.get(f"{top}/Chart.yaml")
    if raw is None:
        raise ValueError(f"no {top}/Chart.yaml")
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"{top}/Chart.yaml is not YAML: {exc}") from exc
    if not isinstance(doc, dict) or not doc.get("name") or not doc.get("version"):
        raise ValueError(f"{top}/Chart.yaml has no name / version")
    return doc


def diff_charts(local: Dict[str, bytes], published: Dict[str, bytes]) -> List[str]:
    """Files that differ: Chart.yaml compared as YAML, everything else as bytes."""
    import yaml
    out = []
    for path in sorted(set(local) | set(published)):
        if path not in published:
            out.append(f"{path} (only in the new package)")
        elif path not in local:
            out.append(f"{path} (only in the published package)")
        elif local[path] == published[path]:
            continue
        elif path.rsplit("/", 1)[-1] == "Chart.yaml":
            try:
                same = yaml.safe_load(local[path]) == yaml.safe_load(published[path])
            except yaml.YAMLError:
                same = False
            if not same:
                out.append(path)
        else:
            out.append(path)
    return out


def judge_chart(repo: registry.Repository, reference: str, local: Dict[str, bytes]) -> Decision:
    where = f"oci://{repo.host}/{repo.repository}:{reference}"
    found = repo.manifest(reference)
    if found is None:
        return Decision(True, f"{where} is not published yet — publish")
    if found.media_type != "application/vnd.oci.image.manifest.v1+json":
        raise registry.NoAnswer(f"{where}: {found.media_type} is not a helm chart manifest")
    layers = found.document.get("layers")
    chart = [layer for layer in layers if isinstance(layer, dict)
             and layer.get("mediaType") == CHART_LAYER] if isinstance(layers, list) else []
    if len(chart) != 1 or not isinstance(chart[0].get("digest"), str):
        raise registry.NoAnswer(f"{where}: expected one {CHART_LAYER} layer, found {len(chart)}")
    blob = repo.blob(chart[0]["digest"], size=chart[0].get("size"), max_bytes=MAX_CHART_BYTES)
    try:
        published = chart_files(blob)
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        raise registry.NoAnswer(f"{where}: the published chart cannot be read: {exc}") from exc
    differing = diff_charts(local, published)
    if differing:
        raise Violation(
            f"{where} is already published with different content:\n"
            + "".join(f"      {d}\n" for d in differing)
            + "   An already-published version cannot be re-pushed — bump the chart version.")
    return Decision(False, f"{where} is already published with the same content — not re-pushed")


# ── CLI ─────────────────────────────────────────────────────────────────────


def _credentials() -> Optional[registry.Credentials]:
    user, password = os.environ.get("REGISTRY_USERNAME", ""), os.environ.get("REGISTRY_PASSWORD", "")
    if bool(user) != bool(password):
        raise CallerError("set both REGISTRY_USERNAME and REGISTRY_PASSWORD, or neither")
    return registry.Credentials(user, password) if user else None


def write_output(**fields: str) -> None:
    """Append `key=value` lines to $GITHUB_OUTPUT (when it is set)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(f"{k}={v}\n" for k, v in fields.items()))


def _write_output(decision: Decision, with_digest: bool) -> None:
    fields = {"publish": "true" if decision.publish else "false"}
    if with_digest:
        fields["digest"] = decision.digest
    write_output(**fields)


def repository(ref: str, timeout: float) -> tuple:
    """(Repository, reference) for *ref*, with $REGISTRY_* credentials."""
    kwargs = {"transport": TRANSPORT} if TRANSPORT is not None else {}
    try:
        return registry.Repository.of(ref, timeout=timeout, credentials=_credentials(), **kwargs)
    except registry.InvalidRef as exc:
        raise CallerError(str(exc)) from exc


def decide_image(ref: str, revision: str, candidate: str, timeout: float) -> Decision:
    if not _COMMIT.fullmatch(revision or ""):
        raise CallerError(f"--revision {revision!r} is not a full commit id")
    if candidate and not _DIGEST.fullmatch(candidate):
        raise CallerError(f"candidate digest {candidate!r} is not sha256:<hex>")
    if "@" in ref:
        raise CallerError(f"{ref!r} must name a tag, not a digest")
    repo, reference = repository(ref, timeout)
    return judge_image(repo, reference, revision, candidate)


def load_chart(tgz: str) -> tuple:
    """(files, Chart.yaml) of a local chart archive; CallerError when unreadable."""
    try:
        local = chart_files(Path(tgz).read_bytes())
        return local, _chart_yaml(local)
    except ImportError as exc:
        raise CallerError(f"PyYAML is required to compare Chart.yaml: {exc}") from exc
    except (OSError, tarfile.TarError, EOFError, ValueError) as exc:
        raise CallerError(f"{tgz}: not a readable chart archive: {exc}") from exc


def decide_chart(tgz: str, oci_registry: str, timeout: float) -> Decision:
    local, meta = load_chart(tgz)
    base = oci_registry[len("oci://"):] if oci_registry.startswith("oci://") else ""
    if not base or "/" not in base or ":" in base.split("/", 1)[1]:
        raise CallerError(f"--registry {oci_registry!r} is not oci://<host>/<path>")
    reference = str(meta["version"]).replace("+", "_")
    repo, _ = repository(f"{base}/{meta['name']}:{reference}", timeout)
    return judge_chart(repo, reference, local)


def _decide(args: argparse.Namespace) -> Decision:
    if args.command == "image":
        return decide_image(args.ref, args.revision, args.candidate_digest or "", args.timeout)
    return decide_chart(args.tgz, args.registry, args.timeout)


def main(argv: Optional[List[str]] = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "發布前問 registry：這個版號已發布了嗎、內容是什麼。沒有 → 發布；"
            "相同（映像：同一個 commit；chart：逐檔相同）→ 不重發；不同 → 失敗。",
            "Ask the registry before publishing whether this version is already there "
            "and what it holds. Absent → publish; the same (image: same commit; chart: "
            "same files) → do not re-publish; different → fail."),
    )
    parser.add_argument("--timeout", type=float, default=30.0,
                        help=i18n_text("每個 HTTP 請求的逾時秒數", "seconds per HTTP request"))
    sub = parser.add_subparsers(dest="command", required=True)
    image = sub.add_parser("image", help=i18n_text("容器映像的正式 tag", "a container image's release tag"))
    image.add_argument("--ref", required=True, help="<registry>/<repository>:<tag>")
    image.add_argument("--revision", required=True,
                       help=i18n_text("要發布的 commit（github.sha）", "the commit being released (github.sha)"))
    image.add_argument("--candidate-digest", default="",
                       help=i18n_text("要 promote 的候選 digest", "the candidate digest to be promoted"))
    chart = sub.add_parser("chart", help=i18n_text("helm chart 的 OCI 版號", "a helm chart's OCI version"))
    chart.add_argument("--tgz", required=True, help=i18n_text("helm package 的產物", "the helm package output"))
    chart.add_argument("--registry", required=True, help="oci://<registry>/<path>")
    args = parser.parse_args(argv)
    try:
        decision = _decide(args)
    except CallerError as exc:
        print(f"❌ release immutability check could not run: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    except registry.NoAnswer as exc:
        print(f"❌ release immutability check got no answer: {exc}", file=sys.stderr)
        print("   This is 'could not ask', not 'not published' — nothing is published.",
              file=sys.stderr)
        return EXIT_CALLER_ERROR
    except Violation as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return EXIT_VIOLATION
    _write_output(decision, with_digest=args.command == "image")
    print(f"{'✅ publish' if decision.publish else '⏭️  skip'}: {decision.message}")
    return EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
