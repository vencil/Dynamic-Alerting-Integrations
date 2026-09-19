#!/usr/bin/env python3
"""generate_crd_schemas.py — vendor the CRD JSON Schemas that the doc gate validates against.

`check_md_yaml_drift.py --check crd` holds every k8s object embedded in `docs/`
to the real CRD contract. That gate needs schemas, and where the schemas come
from is the whole design question, so it is answered here rather than inside the
gate:

* **Upstream CRD, not a catalog.** datreeio/CRDs-catalog is the tool-chain's
  usual answer and it is a DERIVED copy with holes — measured:
  ``monitoring.coreos.com/alertmanagerconfig_v1beta1.json`` is a 404 there, and
  v1beta1 is exactly what six blocks in `docs/` declare.
* **The complete CRD variant.** prometheus-operator ships the same CRD twice;
  ``example/prometheus-operator-crd/`` carries only v1alpha1 while
  ``-crd-full`` carries both. Pointing at the wrong one does not fail — it
  silently yields "no schema" for those six objects, which is why the gate
  treats a missing schema as a VIOLATION rather than a skip.
* **Pinned refs only.** `main` / `latest` cannot be reproduced, and a gate whose
  oracle drifts silently is not an oracle. `SOURCES.yaml` rejects both.

Provenance lives in `docs/schemas/crd/GENERATED.json`: per source the resolved
URL, the sha256 of the bytes fetched, the date, and the files written. `--check`
re-fetches and compares, so "did upstream move under us?" is answerable, and
answered by a command rather than by memory. It needs the network and is
therefore a `make` target, NEVER a pre-commit hook — the gate that runs on every
commit reads only the vendored files.

Exit codes: 0 ok; 1 drift (``--check`` only); 2 caller error (fetch/parse failed).

用法:
  python3 scripts/tools/dx/generate_crd_schemas.py            # 重新產生 vendored schema
  python3 scripts/tools/dx/generate_crd_schemas.py --check    # 只比對上游是否移動過
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(EXIT_CALLER_ERROR)

# ⛔ YAML 1.1 keeps a `=` value tag, and prometheus-operator's AlertmanagerConfig
# CRD contains a literal `- =`. Without this constructor `safe_load` raises
# "could not determine a constructor for the tag 'tag:yaml.org,2002:value'" on
# the ONE source that matters most here. Registering it on SafeLoader keeps the
# safe loader (no arbitrary object construction) while treating `=` as a scalar.
yaml.SafeLoader.add_constructor(
    "tag:yaml.org,2002:value", lambda loader, node: loader.construct_scalar(node)
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CRD_DIR = REPO_ROOT / "docs" / "schemas" / "crd"
SOURCES = CRD_DIR / "SOURCES.yaml"
LOCK = CRD_DIR / "GENERATED.json"
TIMEOUT = 60

# A ref that cannot be reproduced is not a pin. Checked rather than trusted to
# the author's care, because the failure is silent: the schema simply becomes a
# different schema one day and the gate's verdict changes with it.
UNPINNED = ("/main/", "/master/", "/HEAD/", "/latest/", "releases/latest/")


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        return resp.read()


def _schemas_from(raw: bytes, wanted: List[Dict]) -> Tuple[Dict[Tuple[str, str, str], dict], List[Dict]]:
    """Pull the requested (group, kind, version) openAPIV3Schemas out of one CRD file."""
    want = {(w["group"], w["kind"], w["version"]) for w in wanted}
    found: Dict[Tuple[str, str, str], dict] = {}
    for doc in yaml.safe_load_all(raw.decode("utf-8")):
        if not isinstance(doc, dict) or doc.get("kind") != "CustomResourceDefinition":
            continue
        group = doc["spec"]["group"]
        kind = doc["spec"]["names"]["kind"]
        for ver in doc["spec"].get("versions", []):
            key = (group, kind, ver["name"])
            if key not in want:
                continue
            schema = (ver.get("schema") or {}).get("openAPIV3Schema")
            if schema:
                found[key] = schema
    missing = [w for w in wanted
               if (w["group"], w["kind"], w["version"]) not in found]
    return found, missing


def out_path(group: str, kind: str, version: str) -> Path:
    return CRD_DIR / group / f"{kind}_{version}.json"


def run(check_only: bool) -> int:
    if not SOURCES.exists():
        print(f"ERROR: missing {SOURCES}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    decl = yaml.safe_load(SOURCES.read_text(encoding="utf-8"))
    sources = decl.get("sources") or []

    previous = {}
    if LOCK.exists():
        try:
            previous = json.loads(LOCK.read_text(encoding="utf-8")).get("sources", {})
        except (json.JSONDecodeError, OSError):
            previous = {}

    lock: Dict[str, dict] = {}
    drifted: List[str] = []
    written = 0

    for src in sources:
        sid, url = src["id"], src["url"]
        if any(tok in url for tok in UNPINNED):
            print(f"ERROR: {sid}: url is not pinned to a version: {url}", file=sys.stderr)
            return EXIT_CALLER_ERROR
        try:
            raw = _fetch(url)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            print(f"ERROR: {sid}: fetch failed ({type(e).__name__}: {e})", file=sys.stderr)
            return EXIT_CALLER_ERROR

        digest = hashlib.sha256(raw).hexdigest()
        try:
            found, missing = _schemas_from(raw, src["kinds"])
        except yaml.YAMLError as e:
            print(f"ERROR: {sid}: CRD did not parse ({e})", file=sys.stderr)
            return EXIT_CALLER_ERROR
        if missing:
            names = ", ".join(f"{m['group']}/{m['version']} {m['kind']}" for m in missing)
            print(f"ERROR: {sid}: source does not carry {names} — wrong CRD variant or ref?",
                  file=sys.stderr)
            return EXIT_CALLER_ERROR

        outputs = []
        for (group, kind, version), schema in sorted(found.items()):
            p = out_path(group, kind, version)
            # Trailing newline is not cosmetic: the `file-hygiene` pre-commit
            # hook appends one, and without it here every `--check` run after a
            # commit would report drift against a file nothing had changed.
            body = json.dumps(schema, separators=(",", ":"), sort_keys=True,
                              ensure_ascii=False) + "\n"
            rel = str(p.relative_to(REPO_ROOT)).replace(os.sep, "/")
            outputs.append(rel)
            existing = p.read_text(encoding="utf-8") if p.exists() else None
            if existing != body:
                if check_only:
                    drifted.append(f"{rel} (content differs from upstream {src['ref']})")
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(body, encoding="utf-8", newline="\n")
                    written += 1

        was = previous.get(sid, {}).get("sha256")
        if check_only and was and was != digest:
            drifted.append(f"{sid}: upstream bytes changed at the SAME ref {src['ref']} "
                           f"({was[:12]} -> {digest[:12]})")
        lock[sid] = {
            "url": url, "ref": src["ref"], "sha256": digest,
            "fetched": _dt.date.today().isoformat(), "outputs": sorted(outputs),
        }

    if check_only:
        if drifted:
            print("CRD schema drift:")
            for d in drifted:
                print(f"  - {d}")
            print("\nRun `make crd-schemas` to re-vendor, then review the diff.")
            return EXIT_VIOLATION
        print(f"✓ {len(sources)} CRD sources match their vendored schemas.")
        return EXIT_OK

    LOCK.write_text(
        json.dumps({"sources": lock}, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n")
    total = sum(len(v["outputs"]) for v in lock.values())
    print(f"✓ {total} CRD schemas vendored from {len(sources)} pinned sources "
          f"({written} file(s) changed).")
    return EXIT_OK


def main() -> int:
    try_utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="Only report drift between upstream and the vendored copies")
    args = ap.parse_args()
    return run(args.check)


if __name__ == "__main__":
    sys.exit(main())
