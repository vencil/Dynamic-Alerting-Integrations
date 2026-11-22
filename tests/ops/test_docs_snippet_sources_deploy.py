"""Every file the docs include with pymdownx.snippets redeploys the site
(#4099): a snippet from outside docs/ (docs/custom-rule-governance §4.1
includes .github/custom-rule-policy.yaml) is rendered into Pages, so a push
that changes only it must trigger .github/workflows/mkdocs-deploy.yaml —
otherwise Pages keeps serving the old content.

That the file is there is the build's to judge, not this test's (#4152):
mkdocs.yml sets pymdownx.snippets check_paths, so a missing snippet fails
the strict build docs-ci runs on every PR instead of rendering as an empty
block. The second test holds mkdocs.yml to that."""
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
    patterns = _deploy_paths()
    missing = sorted(s for s in sources if not _covered(s, patterns))
    assert not missing, (f"included by the docs but not in mkdocs-deploy.yaml on.push.paths: "
                         f"{missing}")


class _AnyTagLoader(yaml.SafeLoader):
    """mkdocs.yml carries !!python/name tags; read them as plain values."""


_AnyTagLoader.add_multi_constructor("", lambda loader, suffix, node: None)


def test_a_missing_snippet_fails_the_build():
    conf = yaml.load((REPO / "mkdocs.yml").read_text(encoding="utf-8"), Loader=_AnyTagLoader)
    snippets = [e["pymdownx.snippets"] for e in conf["markdown_extensions"]
                if isinstance(e, dict) and "pymdownx.snippets" in e]
    assert len(snippets) == 1, f"pymdownx.snippets is configured {len(snippets)} times"
    assert (snippets[0] or {}).get("check_paths") is True, (
        "mkdocs.yml must set pymdownx.snippets check_paths: true — without it a snippet "
        "whose file is gone renders as an empty block and the strict build passes")
