#!/usr/bin/env python3
"""Init chart-pin gate — the chart `da-tools init` pins must be the chart in the tree.

WHY (#1425): `init` prints `helm upgrade --install … --version <pin>` with
settings written against THIS tree's chart (`--set
thresholdConfig.existingConfigMap=…`). The pin is
`THRESHOLD_EXPORTER_CHART_VERSION`, held equal to `Chart.yaml` `version` by
`test_the_chart_pin_is_the_charts_own_version` — but feature PRs change the
chart without bumping that version, so between releases the pin names an
already-PUBLISHED chart that lacks what the tree's chart has. Published 2.9.0
has no values schema: it takes `existingConfigMap`, ignores it, and renders a
second writer of `threshold-config`. The release playbook pushes `exporter/v*`
together with `tools/v*`, and also says to push only the lines that changed;
nothing stopped a `tools/v*`-only release from shipping a da-tools image whose
instructions the pinned chart silently ignores.

HOW: the pin's chart is `exporter/v<pin>`. If that tag exists, every file
under `helm/threshold-exporter/` that `helm package` would pack must be
identical at HEAD and at the tag; any difference means the chart changed
since it was published, so the release must bump `--exporter` and push
`exporter/v*` as well. Comparing the whole packed chart, not a list of keys,
is the point: a hand-kept list of "keys the published chart lacks" is a
second copy that drifts. Files HEAD's `.helmignore` excludes (the chart
README, `app/`, `config/`) are not in the `.tgz`, so changing them alone does
not hold a release back; `.helmignore` itself ships, so editing it does.

If the tag does not exist, the pin names a chart this release is about to
publish (the wrap-up PR bumped `--exporter`):
  * default (`make pre-tag`, before any tag is pushed): fine — but only after
    `git ls-remote` confirms the tag is absent on origin too, since a shallow
    clone without tags would otherwise read as "not published yet";
  * `--require-tag` (release.yaml's `tools/v*` job, which runs after the
    playbook pushed `exporter/v*` first): a violation, because nothing will
    publish that chart.

⚠️ Scope: a tag is not a published chart. If the `exporter/v*` job fails
before its `helm push`, this gate still passes the `tools/v*` job; that
window is the playbook's "do not announce da-tools until the exporter job
finished" rule, not this gate's.

Exit codes (scripts/tools/_lib_exitcodes.py): 0 clean / 1 finding /
2 caller error (pin unreadable, not a git checkout, origin unreachable).
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
INIT_PY = Path("scripts/tools/ops/init_project.py")
CHART_DIR = "helm/threshold-exporter"
_PIN_RE = re.compile(
    r"^THRESHOLD_EXPORTER_CHART_VERSION = '(\d+\.\d+\.\d+)'$", re.MULTILINE)


class CallerError(Exception):
    """The gate cannot answer; never read as clean."""


def read_pin(root: Path) -> str:
    """The init chart pin, read as text: the gate must not import init."""
    try:
        text = (root / INIT_PY).read_text(encoding="utf-8")
    except OSError as exc:
        raise CallerError(f"cannot read {INIT_PY}: {exc}") from exc
    found = _PIN_RE.findall(text)
    if len(found) != 1:
        raise CallerError(
            f"expected exactly one `THRESHOLD_EXPORTER_CHART_VERSION = 'X.Y.Z'` "
            f"in {INIT_PY}, found {len(found)}")
    return found[0]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True,
                              text=True, encoding="utf-8", timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CallerError(f"git {' '.join(args)}: {exc}") from exc


def local_tag_commit(root: Path, tag: str) -> str | None:
    proc = _git(root, "rev-parse", "--verify", "-q", f"refs/tags/{tag}^{{commit}}")
    if proc.returncode == 0:
        return proc.stdout.strip()
    if _git(root, "rev-parse", "--git-dir").returncode != 0:
        raise CallerError(f"{root} is not a git checkout")
    return None


def tag_on_origin(root: Path, tag: str) -> bool:
    proc = _git(root, "ls-remote", "--tags", "--exit-code", "origin", f"refs/tags/{tag}")
    if proc.returncode == 0:
        return True
    if proc.returncode == 2:  # --exit-code: reachable, no matching ref
        return False
    raise CallerError(f"git ls-remote origin failed (rc {proc.returncode}): "
                      f"{proc.stderr.strip()}")


def helmignore_rules(root: Path) -> list[str]:
    """HEAD's `.helmignore` lines, minus comments, blanks and negations.

    A negation never un-ignores a path an earlier rule matched (helm's
    `Ignore`), so dropping it can only count a file as packed — the
    direction that holds a release back, never the one that lets one through.
    """
    path = root / CHART_DIR / ".helmignore"
    if not path.is_file():
        return []
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith(("#", "!"))]


def is_packed(rel: str, rules: list[str]) -> bool:
    """Whether `helm package` keeps `rel` (a path relative to the chart root).

    helm (pkg/ignore/rules.go) tests each directory on the way down, then the
    file: a trailing `/` rule matches directories only, a rule with no slash
    matches a basename at any depth, any other rule matches the path from the
    chart root (a leading `/` only anchors it there).
    """
    parts = rel.split("/")
    for depth in range(len(parts)):
        is_dir = depth < len(parts) - 1
        for raw in rules:
            if raw.endswith("/") and not is_dir:
                continue
            rule = raw.strip("/")
            target = "/".join(parts[:depth + 1]) if "/" in rule else parts[depth]
            if fnmatch.fnmatchcase(target, rule):
                return False
    return True


def changed_files(root: Path, commit: str) -> list[str]:
    proc = _git(root, "diff", "--name-only", commit, "HEAD", "--", CHART_DIR)
    if proc.returncode != 0:
        raise CallerError(f"git diff {commit} HEAD failed: {proc.stderr.strip()}")
    rules = helmignore_rules(root)
    prefix = CHART_DIR + "/"
    return [line for line in proc.stdout.splitlines()
            if line and is_packed(line[len(prefix):], rules)]


def check(root: Path, require_tag: bool) -> tuple[int, list[str]]:
    pin = read_pin(root)
    tag = f"exporter/v{pin}"
    commit = local_tag_commit(root, tag)
    if commit is None:
        if require_tag:
            return EXIT_VIOLATION, [
                f"FAIL: init pins chart {pin} but tag {tag} does not exist, "
                f"so nothing will publish that chart for this da-tools "
                f"release. Push {tag} first (release playbook Step 3)."]
        if tag_on_origin(root, tag):
            raise CallerError(
                f"{tag} exists on origin but not locally: fetch tags "
                f"(`git fetch --tags origin`) and re-run")
        return EXIT_OK, [
            f"OK: init pins chart {pin}; {tag} is not published yet, so this "
            f"release must push it before tools/v* (release playbook Step 3)."]
    changed = changed_files(root, commit)
    if changed:
        return EXIT_VIOLATION, [
            f"FAIL: init pins chart {pin}, but {CHART_DIR}/ changed since {tag} "
            f"was published; a da-tools image cut now would print settings the "
            f"published chart does not have. Bump the chart "
            f"(`bump_docs.py --exporter <next>`) and release exporter/v* too.",
            *(f"  changed: {path}" for path in changed)]
    return EXIT_OK, [f"OK: {CHART_DIR}/ is identical to {tag} (init pin {pin})."]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "檢查 da-tools init 釘的 threshold-exporter chart 就是樹裡的 chart："
            "helm/threshold-exporter/ 在 exporter/v<pin> 之後有改動時失敗",
            "Check that the threshold-exporter chart da-tools init pins is the "
            "chart in the tree: fail when helm/threshold-exporter/ changed since "
            "exporter/v<pin>"))
    parser.add_argument("--root", type=Path, default=REPO_ROOT,
                        help=i18n_text("repo 根目錄（預設：這個 checkout）",
                                       "repository root (default: this checkout)"))
    parser.add_argument("--require-tag", action="store_true",
                        help=i18n_text(
                            "釘住的 chart 的 exporter/v* tag 必須已存在"
                            "（release.yaml 的 tools/v* job）",
                            "the pinned chart's exporter/v* tag must already "
                            "exist (release.yaml's tools/v* job)"))
    args = parser.parse_args(argv)
    try:
        rc, lines = check(args.root, args.require_tag)
    except CallerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    print("\n".join(lines), file=sys.stderr if rc else sys.stdout)
    return rc


if __name__ == "__main__":
    sys.exit(main())
