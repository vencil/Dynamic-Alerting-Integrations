"""Every file the docs include with pymdownx.snippets is there and redeploys
the site (#4099): a snippet from outside docs/ (docs/custom-rule-governance §4.1
includes .github/custom-rule-policy.yaml) is rendered into Pages, so a push
that changes only it must trigger .github/workflows/mkdocs-deploy.yaml —
otherwise Pages keeps serving the old content."""
from __future__ import annotations

import fnmatch
import pathlib
import re
import sys

import yaml

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from _tree import repo_files  # noqa: E402

REPO = HERE.parents[1]
SNIPPET = re.compile(r'^\s*--8<--\s+"([^"]+)"\s*$', re.M)


def _deploy_paths() -> "list[str]":
    wf = yaml.safe_load((REPO / ".github/workflows/mkdocs-deploy.yaml").read_text(encoding="utf-8"))
    on = wf.get("on", wf.get(True))   # PyYAML reads the key `on` as True
    return on["push"]["paths"]


def _covered(path: str, patterns: "list[str]") -> bool:
    # GitHub's `**` crosses `/`; fnmatch's `*` already does
    return any(fnmatch.fnmatchcase(path, p.replace("**", "*")) for p in patterns)


def test_every_snippet_source_redeploys_the_site():
    sources = {m for f in repo_files(".md") if f.relative_to(REPO).parts[0] == "docs"
               for m in SNIPPET.findall(f.read_text(encoding="utf-8"))}
    assert ".github/custom-rule-policy.yaml" in sources, sources   # tripwire: the scan reads
    # pymdownx.snippets renders a missing file as an empty block, silently
    # (mkdocs.yml does not set check_paths): each source must be there
    gone = sorted(s for s in sources if not (REPO / s).is_file())
    assert not gone, f"included by the docs but not in the tree: {gone}"
    patterns = _deploy_paths()
    missing = sorted(s for s in sources if not _covered(s, patterns))
    assert not missing, (f"included by the docs but not in mkdocs-deploy.yaml on.push.paths: "
                         f"{missing}")
