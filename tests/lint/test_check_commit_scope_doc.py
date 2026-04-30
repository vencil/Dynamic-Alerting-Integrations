"""Tests for check_commit_scope_doc.py — commit scope drift gate.

Covers:
  - `extract_doc_scopes` parses bold list-items in §Scope
  - `extract_doc_scopes` ignores bolds outside list-items (callouts, body)
  - `extract_doc_scopes` stops at the next §heading (doesn't bleed into
    sibling sections)
  - `extract_sot_scopes` parses `.commitlintrc.yaml` rules.scope-enum.[2]
  - `extract_sot_scopes` raises on malformed yaml
  - `compute_drift` correctly classifies illegal vs unmentioned
  - `main` exits 0 on no Type A drift
  - `main` exits 1 on Type A drift with --ci
  - `main` exits 0 on Type A drift WITHOUT --ci (report-only mode)
  - `main` exits 2 on missing files
  - `main` --json output is well-formed
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
sys.path.insert(0, _TOOLS_DIR)

import check_commit_scope_doc as ccs  # noqa: E402


# ---------------------------------------------------------------------------
# extract_doc_scopes
# ---------------------------------------------------------------------------
class TestExtractDocScopes:
    def test_simple_single_bold_per_line(self, tmp_path):
        doc = tmp_path / "convention.md"
        doc.write_text(
            "# Title\n"
            "## Format\n"
            "### Scope\n"
            "Some intro text.\n"
            "\n"
            "- **exporter**: code\n"
            "- **tools**: tools\n"
            "- **docs**: docs\n"
            "\n"
            "### Description\n"
            "- **ignored**: this is in a different section\n",
            encoding="utf-8",
        )
        scopes = ccs.extract_doc_scopes(doc)
        assert scopes == {"exporter", "tools", "docs"}

    def test_multiple_bolds_in_one_line(self, tmp_path):
        doc = tmp_path / "convention.md"
        doc.write_text(
            "### Scope\n"
            "- **ops** / **dx** / **lint**: tool-map sub-categories\n"
            "- **phase-a** / **phase-b** / **phase-c**: phase scopes\n"
            "## End\n",
            encoding="utf-8",
        )
        scopes = ccs.extract_doc_scopes(doc)
        assert scopes == {"ops", "dx", "lint", "phase-a", "phase-b", "phase-c"}

    def test_ignores_bold_in_callout_body(self, tmp_path):
        """Bolds outside `- ` list-items must NOT count as scopes.
        Callouts like '> **Common pitfall**:' are emphasis, not scope defs.
        """
        doc = tmp_path / "convention.md"
        doc.write_text(
            "### Scope\n"
            "- **exporter**: real scope\n"
            "\n"
            "> **Common pitfall**: typing the verbose name. **threshold-exporter** is wrong.\n"
            "\n"
            "Paragraph with **inline-emphasis** also ignored.\n"
            "## End\n",
            encoding="utf-8",
        )
        scopes = ccs.extract_doc_scopes(doc)
        assert scopes == {"exporter"}

    def test_stops_at_next_section_heading(self, tmp_path):
        """Once we hit `### Description` we must stop scanning — bold list-items
        in subsequent sections are NOT scopes."""
        doc = tmp_path / "convention.md"
        doc.write_text(
            "### Scope\n"
            "- **real**: in scope\n"
            "### Description\n"
            "- **fake**: this is in a different section, must be ignored\n"
            "## Examples\n"
            "- **also-fake**: even further away\n",
            encoding="utf-8",
        )
        scopes = ccs.extract_doc_scopes(doc)
        assert scopes == {"real"}

    def test_handles_hyphens_and_plus_in_scope_names(self, tmp_path):
        """Scope names may contain `-` (rule-packs, phase-a) and `+`
        (dx+e2e, lint+tooling for compound scopes); both must extract."""
        doc = tmp_path / "convention.md"
        doc.write_text(
            "### Scope\n"
            "- **rule-packs**: rule pack defs\n"
            "- **session-init**: session startup\n"
            "- **dx+e2e**: compound DX + e2e scope\n"
            "- **lint+tooling**: compound lint + tooling scope\n"
            "## End\n",
            encoding="utf-8",
        )
        scopes = ccs.extract_doc_scopes(doc)
        assert "rule-packs" in scopes
        assert "session-init" in scopes
        assert "dx+e2e" in scopes
        assert "lint+tooling" in scopes

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ccs.extract_doc_scopes(tmp_path / "nonexistent.md")

    def test_no_scope_section_returns_empty(self, tmp_path):
        doc = tmp_path / "convention.md"
        doc.write_text(
            "# Title\n"
            "## Format\n"
            "Just intro, no Scope section.\n",
            encoding="utf-8",
        )
        scopes = ccs.extract_doc_scopes(doc)
        assert scopes == set()


# ---------------------------------------------------------------------------
# extract_sot_scopes
# ---------------------------------------------------------------------------
class TestExtractSotScopes:
    def test_parses_well_formed_yaml(self, tmp_path):
        yaml_path = tmp_path / ".commitlintrc.yaml"
        yaml_path.write_text(
            "extends:\n"
            "  - '@commitlint/config-conventional'\n"
            "rules:\n"
            "  scope-enum:\n"
            "    - 2\n"
            "    - always\n"
            "    - - exporter\n"
            "      - tools\n"
            "      - docs\n"
            "  subject-case:\n"
            "    - 0\n",
            encoding="utf-8",
        )
        scopes = ccs.extract_sot_scopes(yaml_path)
        assert scopes == {"exporter", "tools", "docs"}

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ccs.extract_sot_scopes(tmp_path / "nonexistent.yaml")

    def test_missing_scope_enum_raises(self, tmp_path):
        yaml_path = tmp_path / ".commitlintrc.yaml"
        yaml_path.write_text("rules: {}\n", encoding="utf-8")
        with pytest.raises(ValueError):
            ccs.extract_sot_scopes(yaml_path)

    def test_malformed_root_raises(self, tmp_path):
        yaml_path = tmp_path / ".commitlintrc.yaml"
        # Top-level is a list, not a mapping
        yaml_path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
        with pytest.raises(ValueError):
            ccs.extract_sot_scopes(yaml_path)


# ---------------------------------------------------------------------------
# compute_drift
# ---------------------------------------------------------------------------
class TestComputeDrift:
    def test_no_drift(self):
        illegal, unmentioned = ccs.compute_drift({"a", "b"}, {"a", "b"})
        assert illegal == set()
        assert unmentioned == set()

    def test_type_a_drift_doc_has_scope_sot_doesnt(self):
        illegal, unmentioned = ccs.compute_drift(
            doc_scopes={"a", "b", "fake"},
            sot_scopes={"a", "b"},
        )
        assert illegal == {"fake"}
        assert unmentioned == set()

    def test_type_b_drift_sot_has_scope_doc_doesnt_mention(self):
        illegal, unmentioned = ccs.compute_drift(
            doc_scopes={"a"},
            sot_scopes={"a", "b", "c"},
        )
        assert illegal == set()
        assert unmentioned == {"b", "c"}

    def test_both_drift_classes_simultaneously(self):
        illegal, unmentioned = ccs.compute_drift(
            doc_scopes={"a", "fake"},
            sot_scopes={"a", "b"},
        )
        assert illegal == {"fake"}
        assert unmentioned == {"b"}


# ---------------------------------------------------------------------------
# main (integration)
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_repo(tmp_path):
    """Return (sot_path, doc_path) under tmp_path with caller-controlled content."""
    sot = tmp_path / ".commitlintrc.yaml"
    doc = tmp_path / "commit-convention.md"
    return sot, doc


class TestMain:
    def _write_sot(self, sot_path, scopes):
        body = "rules:\n  scope-enum:\n    - 2\n    - always\n    - - " + \
               "\n      - ".join(scopes) + "\n"
        sot_path.write_text(body, encoding="utf-8")

    def _write_doc(self, doc_path, scopes):
        lines = ["### Scope", ""]
        for s in scopes:
            lines.append(f"- **{s}**: description")
        lines.append("")
        lines.append("### Description")
        doc_path.write_text("\n".join(lines), encoding="utf-8")

    def test_exit_0_when_no_drift(self, fake_repo, capsys):
        sot, doc = fake_repo
        self._write_sot(sot, ["exporter", "tools"])
        self._write_doc(doc, ["exporter", "tools"])
        rc = ccs.main(["--sot", str(sot), "--doc", str(doc), "--ci"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "PASS" in out
        assert "Type A drift" not in out

    def test_exit_1_on_type_a_drift_in_ci_mode(self, fake_repo, capsys):
        sot, doc = fake_repo
        self._write_sot(sot, ["exporter"])
        self._write_doc(doc, ["exporter", "imaginary-scope"])
        rc = ccs.main(["--sot", str(sot), "--doc", str(doc), "--ci"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "imaginary-scope" in out
        assert "Type A drift" in out

    def test_exit_1_on_type_a_drift_independent_of_ci_flag(self, fake_repo, capsys):
        """Type A drift always returns exit 1 — invariant verification.

        The --ci flag exists for future soft-mode toggling; today both
        modes share the same exit-code semantics. This test asserts the
        invariant ("Type A always fails the gate") rather than locking a
        gap — the behavior is intentional and correct, not transitional.
        """
        sot, doc = fake_repo
        self._write_sot(sot, ["exporter"])
        self._write_doc(doc, ["exporter", "fake"])
        rc = ccs.main(["--sot", str(sot), "--doc", str(doc)])
        assert rc == 1

    def test_exit_0_on_only_type_b_drift(self, fake_repo, capsys):
        """SOT has scope, doc doesn't mention — by design (doc is curated subset).
        Should report but not fail."""
        sot, doc = fake_repo
        self._write_sot(sot, ["exporter", "tools", "extra1", "extra2"])
        self._write_doc(doc, ["exporter", "tools"])
        rc = ccs.main(["--sot", str(sot), "--doc", str(doc), "--ci"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "extra1" in out
        assert "Type B" in out

    def test_exit_2_on_missing_sot(self, fake_repo, capsys):
        sot, doc = fake_repo
        self._write_doc(doc, ["exporter"])
        # sot intentionally not created
        rc = ccs.main(["--sot", str(sot), "--doc", str(doc), "--ci"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "SOT not found" in err or "not found" in err

    def test_exit_2_on_missing_doc(self, fake_repo, capsys):
        sot, doc = fake_repo
        self._write_sot(sot, ["exporter"])
        # doc intentionally not created
        rc = ccs.main(["--sot", str(sot), "--doc", str(doc), "--ci"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "doc not found" in err or "not found" in err

    def test_json_output_well_formed(self, fake_repo, capsys):
        sot, doc = fake_repo
        self._write_sot(sot, ["exporter", "tools", "extra"])
        self._write_doc(doc, ["exporter", "imaginary"])
        rc = ccs.main(["--sot", str(sot), "--doc", str(doc), "--json"])
        assert rc == 1  # imaginary is illegal
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["illegal"] == ["imaginary"]
        assert "tools" in payload["unmentioned"]
        assert "extra" in payload["unmentioned"]
        assert payload["sot_count"] == 3
        assert payload["doc_count"] == 2
        assert payload["drift_a_count"] == 1
        assert payload["drift_b_count"] == 2

    def test_truncates_unmentioned_preview_at_10(self, fake_repo, capsys):
        """Long Type B lists shouldn't spam — preview caps at 10 with summary."""
        sot, doc = fake_repo
        many_extras = [f"extra-{i:02d}" for i in range(15)]
        self._write_sot(sot, ["exporter"] + many_extras)
        self._write_doc(doc, ["exporter"])
        rc = ccs.main(["--sot", str(sot), "--doc", str(doc), "--ci"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "and 5 more" in out  # 15 extras, preview 10, 5 elided
