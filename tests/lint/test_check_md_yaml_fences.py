"""Contract tests for `check_md_yaml_drift.py --check fences`.

Since #3416 this check reports the errors of the docs' YAML inventory
(scripts/tools/_lib_doc_yaml_inventory.py): a block its reader refuses, a
fence attribute outside the vocabulary (tests/lint/test_doc_yaml_inventory.py
pins those). The reader of an undeclared block is da-guard parse-tree, which
conftest builds for the session.

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
        capture_output=True, text=True, encoding="utf-8", timeout=60,
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


def test_a_declared_multi_document_stream_passes(tmp_path: Path) -> None:
    """`---`-separated manifests are kubectl's to read: declared `{.k8s}`,
    da-crdecode reads every document and the fence passes."""
    body = ("```yaml {.k8s}\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: a}\n---\n"
            "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: b}\n```\n")
    r = _run(_docs(tmp_path, "multi.md", body))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr
    assert "k8s (declared)" in r.stdout, r.stdout


def test_an_undeclared_multi_document_stream_asks_for_a_declaration(tmp_path: Path) -> None:
    """#3416: an undeclared fence is read by the conf.d reader (parse-tree),
    which refuses several documents. That says nothing about a manifest, so
    the author is asked to say what the block is — never a silent pass."""
    body = "```yaml\napiVersion: v1\nkind: A\n---\napiVersion: v1\nkind: B\n```\n"
    r = _run(_docs(tmp_path, "multi.md", body))
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "[FENCE] docs/multi.md:2" in r.stdout, r.stdout
    assert "multiple_documents" in r.stdout and "{.k8s}" in r.stdout, r.stdout


def test_directory_tree_in_yaml_fence_fails(tmp_path: Path) -> None:
    body = "```yaml\nconf.d/\n├── _defaults.yaml   # global\n└── a.yaml\n```\n"
    r = _run(_docs(tmp_path, "tree.md", body))
    assert r.returncode == EXIT_VIOLATION, r.stdout
    # The unit's first line (the fence is line 1), as every inventory error.
    assert "[FENCE] docs/tree.md:2" in r.stdout
    assert "syntax_error" in r.stdout, r.stdout


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
    no `key:` anywhere, is a YAML *scalar*: a top level that is not a mapping,
    which is no conf.d file, so it passes.

    "A reader reads it" is necessary, not sufficient, for an honest fence tag. It
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
    assert "docs/loc.md:6" in r.stdout


def test_a_text_pyyaml_reads_but_yaml_v3_refuses_fails(tmp_path: Path) -> None:
    """#3416: the fence check no longer asks PyYAML. `"\\/"` in a double-
    quoted scalar is an escape PyYAML reads and yaml.v3 refuses
    (syntax_error): a block of this repo is read by yaml.v3's readers."""
    body = '```yaml\nurl: "https:\\/\\/x"\n```\n'
    r = _run(_docs(tmp_path, "esc.md", body))
    assert r.returncode == EXIT_VIOLATION, r.stdout
    assert "syntax_error" in r.stdout, r.stdout


def test_a_text_yaml_v3_reads_but_pyyaml_refuses_passes(tmp_path: Path) -> None:
    """The other direction: a TAB after `key:` is refused by PyYAML and read
    by yaml.v3, the reader of these blocks — no error."""
    body = "```yaml\nk:\tv\n```\n"
    r = _run(_docs(tmp_path, "tab.md", body))
    assert r.returncode == EXIT_OK, r.stdout + r.stderr


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
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert r.returncode == EXIT_OK
    assert "--check" in r.stdout
