"""Contract tests for `check_md_yaml_drift.py --check fences`.

Deliberately synthetic (each case builds its own tiny docs/ tree under
``tmp_path``) rather than asserting against the live repo. The live tree is
already guarded by the `md-yaml-fence-check` pre-commit hook, which runs in
ci.yml's always-on Lint job; these tests exist to pin the CHECKER's behaviour,
so they keep working when docs/ legitimately changes — and so they still run
even though the `python` CI filter carries only ``docs/glossary*.md``, not
``docs/**``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "tools" / "lint" / "check_md_yaml_drift.py"
)

EXIT_OK = 0
EXIT_VIOLATION = 1


def _run(repo_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-X", "utf8", str(SCRIPT),
         "--check", "fences", "--ci", "--repo-root", str(repo_root)],
        capture_output=True, text=True, timeout=60,
    )


def _docs(tmp_path: Path, name: str, body: str) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_plain_yaml_fence_passes(tmp_path: Path) -> None:
    root = _docs(tmp_path, "ok.md", "# t\n\n```yaml\ndefaults:\n  a: 1\n```\n")
    r = _run(root)
    assert r.returncode == EXIT_OK, r.stdout + r.stderr


def test_multi_document_stream_passes(tmp_path: Path) -> None:
    """`---`-separated manifests are valid YAML — the reason this check uses
    ``safe_load_all``. Four real blocks in docs/ are exactly this shape, and a
    single-document loader would wrongly demand they be split apart."""
    body = "```yaml\napiVersion: v1\nkind: A\n---\napiVersion: v1\nkind: B\n```\n"
    r = _run(_docs(tmp_path, "multi.md", body))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr


def test_directory_tree_in_yaml_fence_fails(tmp_path: Path) -> None:
    body = "```yaml\nconf.d/\n├── _defaults.yaml   # global\n└── a.yaml\n```\n"
    r = _run(_docs(tmp_path, "tree.md", body))
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "[FENCE] docs/tree.md:1" in r.stdout


def test_shell_mixed_with_yaml_in_one_fence_fails(tmp_path: Path) -> None:
    """The real-world shape: install commands followed by a manifest. Seven
    blocks in docs/ were exactly this before the split."""
    body = (
        "```yaml\n"
        "# 1. install\n"
        "kubectl create secret generic x \\\n"
        "  --from-literal=a=b\n"
        "\n"
        "# 2. reference it\n"
        "env:\n"
        "  - name: A\n"
        "```\n"
    )
    r = _run(_docs(tmp_path, "mixed.md", body))
    assert r.returncode == EXIT_VIOLATION, r.stdout


def test_pure_shell_fence_is_a_known_blind_spot(tmp_path: Path) -> None:
    """⚠️ Stated rather than papered over: a fence containing ONLY shell, with
    no `key:` anywhere, is a valid YAML *scalar* and therefore passes.

    "Parses as YAML" is necessary, not sufficient, for an honest fence tag. It
    catches the mislabels that actually occur here (directory trees, and shell
    mixed with a manifest) because those contain colons that collide. Closing
    this fully would need language detection, which is a different tool with a
    different false-positive profile — deliberately not attempted.
    """
    body = "```yaml\nkubectl get pods -n monitoring\n```\n"
    r = _run(_docs(tmp_path, "pureshell.md", body))
    assert r.returncode == EXIT_OK, (
        "if this ever starts failing the checker gained language detection — "
        "update this test and the docstring rather than reverting it"
    )


def test_failure_names_file_and_line(tmp_path: Path) -> None:
    """A violation must be locatable — the fence's opening line, not the file."""
    body = "# heading\n\nprose\n\n```yaml\na: [1\n```\n"
    r = _run(_docs(tmp_path, "loc.md", body))
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "docs/loc.md:5" in r.stdout


def test_non_yaml_fences_are_ignored(tmp_path: Path) -> None:
    """Only author-tagged yaml is in scope; ```text / ```bash are the fix, so
    they must not themselves be flagged."""
    body = "```text\nconf.d/\n├── a\n```\n\n```bash\nkubectl apply -f x.yaml\n```\n"
    r = _run(_docs(tmp_path, "other.md", body))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr


@pytest.mark.parametrize("flag", ["--help"])
def test_help_exits_zero(flag: str) -> None:
    r = subprocess.run(  # noqa: S603
        [sys.executable, "-X", "utf8", str(SCRIPT), flag],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == EXIT_OK
    assert "--check" in r.stdout
