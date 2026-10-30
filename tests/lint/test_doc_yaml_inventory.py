"""The docs' YAML inventory (scripts/tools/_lib_doc_yaml_inventory.py, #3416).

What these pin:

* the fence attribute vocabulary is closed — every form the module docstring
  names is read, anything else is an error (rustdoc's
  `invalid_codeblock_attributes`), and so is the brace-less spelling mkdocs
  does not render as a code block;
* the two blocks both lints let through before the inventory: a repeated key
  in a block of registry keys only, and a repeated key under a tenant name
  with no marker key — now each is an error of `--check fences`;
* a fence with attributes stays in every gate (the drift checks' own tests,
  the unit-sanity tests and the Go parity below carry the rest of that);
* the Go loader test agrees with the inventory unit for unit
  (`TestDocsYAMLInventoryParity`, run from here so a change to this module
  alone runs it — the Go leg wakes on docs/ and Go, this leg on scripts/).

The readers are the real ones (conftest builds da-guard / da-crdecode /
da-read from this checkout for the session).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
import _lib_doc_yaml_inventory as inv  # noqa: E402
from _da_guard_build import go_build_env  # noqa: E402

DRIFT = REPO / "scripts" / "tools" / "lint" / "check_md_yaml_drift.py"


def _docs(tmp_path: Path, body: str, name: str = "x.md") -> Path:
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / name).write_text(body, encoding="utf-8")
    return tmp_path


def _fences(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-X", "utf8", str(DRIFT), "--check", "fences", "--ci",
         "--repo-root", str(root)],
        capture_output=True, text=True, encoding="utf-8", timeout=120)


# ── vocabulary ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("info, consumer, reason, refused, invalid", [
    ("{.confd}", "confd", None, None, False),
    ("{.k8s}", "k8s", None, None, False),
    ("{.helm-values}", "helm-values", None, None, False),
    ("{.prom-rules}", "prom-rules", None, None, False),
    ("{.alertmanager}", "alertmanager", None, None, False),
    ("{.prometheus}", "prometheus", None, None, False),
    ('{.illustrative reason="shows a reading"}', None, "shows a reading", None, False),
    ("{.confd .expect-refused=duplicate_key}", "confd", None, "duplicate_key", False),
    ("{.expect-refused=duplicate_key .confd}", "confd", None, "duplicate_key", False),
    ("{.confd .expect-invalid}", "confd", None, None, True),
    ("{ .confd }", "confd", None, None, False),
])
def test_every_known_form_is_read(info, consumer, reason, refused, invalid):
    d = inv.parse_declaration(info)
    assert d.errors == [], d.errors
    assert (d.consumer, d.illustrative, d.expect_refused, d.expect_invalid) == (
        consumer, reason, refused, invalid)


@pytest.mark.parametrize("info, needle", [
    ("confd", "not an attribute list"),
    (".confd", "not an attribute list"),
    ('title="x"', "not an attribute list"),
    ("{.conf-d}", "unknown attribute '.conf-d'"),
    ("{#an-id}", "cannot read attribute text"),
    ("{.confd=1}", "unknown attribute '.confd=1'"),
    ("{.illustrative}", "needs a non-empty reason"),
    ('{.illustrative reason=""}', "needs a non-empty reason"),
    ('{.illustrative reason="  "}', "needs a non-empty reason"),
    ('{.illustrative .confd reason="r"}', "takes no other attribute"),
    ('{.confd reason="r"}', "only for .illustrative"),
    ("{.confd title=x}", "unknown attribute title="),
    ("{.confd .k8s}", "more than one consumer"),
    ("{.expect-refused=duplicate_key}", "needs .confd"),
    ("{.k8s .expect-refused=x}", "needs .confd"),
    ("{.confd .expect-refused}", "needs a kind"),
    ("{.k8s .expect-invalid}", "needs .confd"),
    ("{.confd .expect-invalid .expect-refused=duplicate_key}", "exclude each other"),
])
def test_anything_else_is_an_error(info, needle):
    d = inv.parse_declaration(info)
    assert any(needle in e for e in d.errors), d.errors


def test_bare_word_fence_is_extracted_and_refused(tmp_path):
    """```` ```yaml confd ```` is still a fence to the extractor (so nothing
    inside it is read as another fence), and an error: mkdocs renders it as
    inline code, not as a code block."""
    root = _docs(tmp_path, "```yaml confd\ndefaults:\n  a: 1\n```\n")
    r = _fences(root)
    assert r.returncode == 1, r.stdout
    assert "[FENCE] docs/x.md:2" in r.stdout and "not an attribute list" in r.stdout


def test_extractor_takes_attributed_and_indented_fences():
    md = ("```yaml {.confd}\na: 1\n```\n"
          "  ```yml {.k8s}\n  b: 2\n  ```\n"
          "```yamlx\nc: 3\n```\n"
          "```yaml\n```\n")
    got = [(f.line, f.info, f.body) for f in inv.extract_fences("x.md", md)]
    assert got == [(1, "{.confd}", ["a: 1"]), (4, "{.k8s}", ["  b: 2"])]


# ── consumers and refusals ───────────────────────────────────────────────────

def test_each_declared_consumer_reads_its_unit(tmp_path):
    root = _docs(tmp_path, (
        "```yaml {.confd}\ntenants:\n  t1:\n    a: \"1\"\n```\n"
        "```yaml {.k8s}\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: a}\n---\n"
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: b}\n```\n"
        "```yaml {.helm-values}\ninstance: 010\n```\n"
        "```yaml {.prom-rules}\ngroups:\n  - name: g\n    rules:\n"
        "      - record: r\n        expr: up\n```\n"
        "```yaml {.alertmanager}\nroute:\n  receiver: r\nreceivers:\n  - name: r\n```\n"
        "```yaml {.prometheus}\nglobal:\n  scrape_interval: 15s\n```\n"))
    units = inv.build(root).units
    assert [u.errors for u in units] == [[]] * 6, [u.errors for u in units]
    assert [u.consumer for u in units] == [
        "confd", "k8s", "helm-values", "prom-rules", "alertmanager", "prometheus"]
    assert all(u.read and u.how == "declared" for u in units)
    assert len(units[1].documents) == 2
    assert units[2].documents == [{"instance": 8}]       # Helm's reading: 010 is octal 8
    assert units[0].documents == [{"tenants": {"t1": {"a": "1"}}}]


def test_a_declared_consumer_that_refuses_is_an_error(tmp_path):
    """Declared, the consumer answers whatever the text is — a refusal is
    the author's to fix (or to declare `.expect-refused`)."""
    root = _docs(tmp_path, "```yaml {.k8s}\na: [1\n```\n```yaml {.confd}\n- a\n```\n")
    units = inv.build(root).units
    assert "da-crdecode refuses it" in units[0].errors[0], units[0].errors
    assert "(top_level_not_mapping)" in units[1].errors[0], units[1].errors


def test_expect_refused_must_hold(tmp_path):
    root = _docs(tmp_path, (
        "```yaml {.confd .expect-refused=duplicate_key}\na: 1\na: 2\n```\n"
        "```yaml {.confd .expect-refused=duplicate_key}\na: 1\n---\nb: 2\n```\n"
        "```yaml {.confd .expect-refused=duplicate_key}\na: 1\n```\n"))
    units = inv.build(root).units
    assert units[0].errors == []
    assert "refuses it as multiple_documents" in units[1].errors[0], units[1].errors
    assert "but da-guard parse-tree reads it" in units[2].errors[0], units[2].errors


def test_an_illustrative_fence_is_read_by_nobody(tmp_path):
    root = _docs(tmp_path, '```yaml {.illustrative reason="r"}\na: [1\n```\n')
    u, = inv.build(root).units
    assert (u.errors, u.read, u.refusal, u.exemption) == ([], False, None, "r")


def test_undeclared_units_are_judged_by_structure(tmp_path):
    root = _docs(tmp_path, (
        "```yaml\ndefaults:\n  a: 1\n```\n"
        "```yaml\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: a}\n```\n"
        "```yaml\nscrape_configs: []\n```\n"
        "```yaml\n- alert: A\n```\n"))
    units = inv.build(root).units
    assert [(u.consumer, u.how, u.read, u.errors) for u in units] == [
        ("confd", "structure", True, []),
        ("k8s", "structure", True, []),
        (None, None, True, []),
        (None, None, False, []),     # top level not a mapping: no conf.d file
    ]
    assert units[0].confd_kind == "platform"


def test_retired_marker_is_an_error(tmp_path):
    root = _docs(tmp_path, "<!-- md-yaml-drift: ignore — old -->\n```yaml\na: 1\n```\n")
    i = inv.build(root)
    assert i.file_errors and i.file_errors[0][:2] == ("docs/x.md", 1)
    assert i.units[0].exemption is None


# ── the two blocks that slipped past both lints before (#3416) ──────────────

@pytest.mark.parametrize("body", [
    # a repeated key in a block holding registry keys only: no conf.d marker,
    # so the drift lint never judged it; PyYAML kept the last value, which is
    # all the unit-sanity lint saw
    "```yaml\nmysql_connections: \"70\"\nmysql_connections: \"700\"\n```\n",
    # a repeated key under a tenant name with no marker key below it
    "```yaml\ntenant-x:\n  mysql_connections: \"70\"\n  mysql_connections: \"700\"\n```\n",
])
def test_a_repeated_key_the_lints_let_through_is_now_an_error(tmp_path, body):
    r = _fences(_docs(tmp_path, body))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "[FENCE] docs/x.md:2" in r.stdout and "duplicate_key" in r.stdout, r.stdout


# ── Go parity ────────────────────────────────────────────────────────────────

def test_go_loader_test_agrees_with_the_inventory():
    """Runs the Go half (pkg/config TestDocsYAMLInventoryParity and the loader
    tests that take their samples from the inventory) against this checkout.
    No go is a failure, not a skip: the parity is the point."""
    go = shutil.which("go")
    if go is None:
        pytest.fail("`go` is not on PATH: the Go half of the inventory parity cannot run")
    env = {**go_build_env(), "CI": "1"}
    if os.environ.get("DA_GUARD_BINARY"):
        env["DA_GUARD_BINARY"] = os.path.abspath(os.environ["DA_GUARD_BINARY"])
    proc = subprocess.run(  # noqa: S603
        [go, "test", "-count=1", "-run",
         "^(TestDocsYAMLInventoryParity|TestExtractYAMLBlocksTakesFenceAttributes|"
         "TestDocsDefaults.*|TestDocsYamlSplit.*)$", "./pkg/config/"],
        cwd=REPO / "components" / "threshold-exporter" / "app",
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=900, env=env, check=False)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
