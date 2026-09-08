#!/usr/bin/env python3
"""check_chart_package_contents.py — assert what a packaged Helm chart .tgz actually contains (#1755).

`check_chart_ship_surface.py` asserts the chart **source tree**: every file at a
shipping chart's root is declared SHIP, or EXCLUDE plus a matching `.helmignore`
line. This asserts the **artifact** — the bytes `helm package` produced and
`helm push` is about to publish to `oci://<registry>/charts`.

Why both exist
--------------
The source-tree gate models what helm will do. The model was measured against a
real `helm package` for all six `.helmignore` spellings that can apply to a root
file and agreed 6/6, so at the chart ROOT the two are near-equivalent. What only
the artifact can show:

1. **Top-level subtrees.** The source gate declares `templates/`, `crds/` and
   `charts/` out of scope entirely. This one pins, per chart, *which* subtrees
   the archive may contain — so a new `charts/` (a dependency pulled in by
   `helm dependency update`, whose whole content then ships) or any directory
   nobody expected turns it red.
   ⚠️ It does NOT enumerate files *inside* those subtrees. A stray `.bak` next
   to a template is not caught; asserting 22 template files one by one would be
   high-churn and low-signal. The boundary is stated so the docstring cannot be
   read as promising more than the code does.
2. **Working-tree contamination at package time.** `helm package` archives the
   directory *as it exists when the command runs*. Anything an earlier step in
   the same job wrote into the chart directory ships. A gate that ran on another
   commit, in another job, is structurally blind to that.
3. **Model drift.** A future helm changes its ignore semantics; the model does
   not notice, the tarball does.

⚠️ On today's tree this gate flags nothing: the three shipping charts hold 22
subdirectory files, all templates, and no chart has dependencies. It is
insurance against the three classes above, not a fix for a present defect. That
is stated here so nobody mistakes a green run for evidence it caught something.

Where it runs
-------------
Between `helm package` and `helm push` — in `.github/workflows/release.yaml`
(three charts) and in the `chart-package` Makefile target. That placement is the
only one that sees the published bytes; `check_chart_ship_surface.py` asserts
that every `helm package` call site is followed by this verification, so the
wiring cannot silently rot.

Exit codes
    0  the archive contains exactly what is declared
    1  violation
    2  caller error — could not RUN the check (no helm, no archive, unknown
       chart). Never a silent pass, never a skip.

Usage
    check_chart_package_contents.py --chart helm/tenant-api --tgz .build/tenant-api-2.9.21.tgz
    check_chart_package_contents.py --chart helm/tenant-api     # packages it itself, needs helm
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess  # nosec B404 — fixed argv, no shell, helm only
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Dict, List, Set

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

# ⭐ Single source of truth: the same table the source-tree gate asserts against.
# Two tables would drift, and the drift would read as "the artifact changed".
from check_chart_ship_surface import DECLARED, SHIP  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]

# Which top-level subtrees each shipping chart's archive may contain. Declared,
# not inferred: `charts/` appearing means a dependency now ships inside the
# package, and that should be a decision someone made, not a surprise. Measured
# from a real `helm package` of each chart — all three carry templates/ only.
DECLARED_SUBTREES: Dict[str, Set[str]] = {
    # #1352 case A: `files` is not decoration. A template renders it with
    # `.Files.Get "files/relay_token.js"`, so a .tgz that lost the subtree
    # would still install and still template — into an nginx ConfigMap with an
    # empty script block.
    "helm/da-portal": {"templates", "files"},
    "helm/threshold-exporter": {"templates"},
    "helm/recipe-preview": {"templates"},
    "helm/tenant-api": {"templates"},
}


class CallerError(Exception):
    """The check could not be RUN — never reported as a pass."""


def archive_members(tgz: Path) -> List[str]:
    """Regular-file paths inside the archive, chart-name prefix included."""
    if not tgz.is_file():
        raise CallerError(f"no such archive: {tgz}")
    try:
        # encoding= is not about file content (a .tgz is binary) — it is how
        # tarfile decodes MEMBER NAMES, and its default follows the filesystem
        # encoding, so a chart packed on one host could read back differently on
        # another. Pinning utf-8 makes the member list deterministic.
        with tarfile.open(tgz, "r:gz", encoding="utf-8") as tar:
            return sorted(m.name for m in tar.getmembers() if m.isfile())
    except (tarfile.TarError, OSError) as exc:
        raise CallerError(f"cannot read {tgz}: {exc}") from exc


def package(chart_dir: Path, dest: Path) -> Path:
    """Run `helm package` and return the .tgz it wrote."""
    helm = shutil.which("helm")
    if helm is None:
        raise CallerError(
            "helm is not on PATH and no --tgz was given, so there is nothing to "
            "inspect. The dev container ships helm "
            "(devcontainer.json: kubectl-helm-minikube), and CI installs it "
            "(azure/setup-helm@v4). This is 'could not measure', not 'measured "
            "and found nothing'."
        )
    try:
        proc = subprocess.run(  # nosec B603 — fixed argv, no shell
            [helm, "package", str(chart_dir), "-d", str(dest)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CallerError(f"helm package failed to run: {exc}") from exc
    if proc.returncode != 0:
        raise CallerError(
            f"helm package {chart_dir} exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    built = sorted(dest.glob("*.tgz"))
    if len(built) != 1:
        raise CallerError(
            f"expected exactly one .tgz in {dest}, found {[p.name for p in built]}"
        )
    return built[0]


def check_archive(chart: str, tgz: Path) -> List[str]:
    """Return violations for one packaged chart. Empty list == pass."""
    declared = DECLARED.get(chart)
    if declared is None:
        raise CallerError(
            f"{chart!r} has no entry in check_chart_ship_surface.DECLARED, so "
            f"there is nothing to compare the archive against"
        )
    expected_root: Set[str] = {n for n, (d, _r) in declared.items() if d == SHIP}

    violations: List[str] = []
    members = archive_members(tgz)
    if not members:
        raise CallerError(f"{tgz} contains no files")

    prefixes = {m.split("/", 1)[0] for m in members}
    if len(prefixes) != 1:
        raise CallerError(
            f"{tgz} has {len(prefixes)} top-level entries {sorted(prefixes)}; "
            f"a helm chart archive holds exactly one"
        )

    expected_subtrees = DECLARED_SUBTREES.get(chart)
    if expected_subtrees is None:
        raise CallerError(
            f"{chart!r} has no entry in DECLARED_SUBTREES, so there is nothing to "
            f"compare the archive's directory layout against"
        )

    actual_root: Set[str] = set()
    actual_subtrees: Set[str] = set()
    for member in members:
        rel = member.split("/", 1)[1] if "/" in member else ""
        if not rel:
            continue
        if "/" not in rel:
            actual_root.add(rel)
        else:
            actual_subtrees.add(rel.split("/", 1)[0])

    for subtree in sorted(actual_subtrees - expected_subtrees):
        n = sum(1 for m in members if m.split("/", 1)[1].startswith(subtree + "/"))
        extra = (
            " `charts/` means a subchart dependency now ships inside this package."
            if subtree == "charts"
            else ""
        )
        violations.append(
            f"{chart}: the archive carries a {subtree!r}/ directory ({n} file(s)) "
            f"that DECLARED_SUBTREES does not list.{extra} Everything under it "
            f"reaches whoever pulls the chart."
        )

    for subtree in sorted(expected_subtrees - actual_subtrees):
        violations.append(
            f"{chart}: {subtree!r}/ is declared but the archive has no such "
            f"directory — drop the stale declaration."
        )

    for name in sorted(actual_root - expected_root):
        extra = (
            " A values-* file next to values.yaml is exactly the #1597 shape."
            if name.startswith("values-")
            else ""
        )
        violations.append(
            f"{chart}: the archive carries {name!r} at the chart root, which is not "
            f"declared {SHIP} in check_chart_ship_surface.DECLARED.{extra} Declare "
            f"it, or keep it out of the package."
        )

    for name in sorted(expected_root - actual_root):
        violations.append(
            f"{chart}: {name!r} is declared {SHIP} but the archive does NOT contain "
            f"it — the declaration promises customers a file they never receive."
        )

    return violations


def main(argv: List[str] | None = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "斷言 helm package 產出的 .tgz 裡實際有什麼：根目錄檔案集合必須等於 "
            "check_chart_ship_surface.DECLARED 標 SHIP 的集合，子目錄只能是 "
            "templates/ crds/ charts/。驗的是要送到客戶手上的位元組，不是原始樹。",
            "Assert what a packaged Helm chart .tgz actually contains: its root "
            "files must equal the SHIP set in check_chart_ship_surface.DECLARED, "
            "and subdirectories may only be templates/, crds/ or charts/. This "
            "inspects the bytes customers receive, not the source tree.",
        ),
    )
    parser.add_argument(
        "--chart",
        required=True,
        help=i18n_text(
            "chart 目錄，寫法需與 helm package 呼叫點一致（例：helm/tenant-api）",
            "chart directory, spelled as the `helm package` call site spells it "
            "(e.g. helm/tenant-api)",
        ),
    )
    parser.add_argument(
        "--tgz",
        help=i18n_text(
            "要檢查的 .tgz；省略時本工具自己跑 helm package（需要 helm）",
            "the .tgz to inspect; omitted, this tool runs `helm package` itself "
            "(requires helm)",
        ),
    )
    args = parser.parse_args(argv)

    chart = args.chart.rstrip("/")
    tmp: tempfile.TemporaryDirectory | None = None
    try:
        if args.tgz:
            tgz = Path(args.tgz)
        else:
            chart_dir = REPO_ROOT / chart
            if not chart_dir.is_dir():
                raise CallerError(f"{chart!r} is not a directory under {REPO_ROOT}")
            tmp = tempfile.TemporaryDirectory(prefix="chart-package-contents-")
            tgz = package(chart_dir, Path(tmp.name))
        violations = check_archive(chart, tgz)
        archive_name = tgz.name
    except CallerError as exc:
        print(
            f"❌ chart package contents check could not run: {exc}", file=sys.stderr
        )
        return EXIT_CALLER_ERROR
    finally:
        if tmp is not None:
            tmp.cleanup()

    if violations:
        print("❌ chart package contents (#1755):", file=sys.stderr)
        for v in violations:
            print(f"   - {v}", file=sys.stderr)
        print(
            "\n   This reads the archive, not the source tree: it is the last place "
            "to\n   notice before `helm push` publishes it to "
            "oci://<registry>/charts.",
            file=sys.stderr,
        )
        return EXIT_VIOLATION

    print(f"✓ {archive_name}: contents match what {chart} declares")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
