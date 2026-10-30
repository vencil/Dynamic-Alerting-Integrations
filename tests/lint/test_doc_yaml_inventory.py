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


@pytest.mark.parametrize("opening", [
    "~~~yaml", "```YAML", "```Yml", "````yaml", "``` yaml", "```yaml+jinja", "~~~ YAML {.confd}",
])
def test_a_yaml_fence_spelled_another_way_is_an_error(tmp_path, opening):
    """#3416 review: every one of these used to fall out of every gate in
    silence. Fences are found as CommonMark finds them; a yaml fence that is
    not ```yaml / ```yml is an error naming the standard spelling."""
    close = opening[:4] if opening.startswith("````") else opening[:3]
    root = _docs(tmp_path, f"{opening}\ntenants:\n  t: {{a: '1'}}\n  t: {{a: '2'}}\n{close}\n")
    i = inv.build(root)
    assert i.units == []
    assert [(p, ln) for p, ln, _e in i.file_errors] == [("docs/x.md", 1)], i.file_errors
    assert "write it as ```yaml" in i.file_errors[0][2]


@pytest.mark.parametrize("body", [
    "```yaml\na: 1\n",                          # a yaml fence never closed
    "```bash\necho\n\n```yaml\na: 1\n",          # a bash fence never closed
    "````yaml\na: 1\n```\n",                     # ``` does not close ````
])
def test_an_unclosed_fence_is_an_error(tmp_path, body):
    i = inv.build(_docs(tmp_path, body))
    assert any("never closed" in e for _p, _l, e in i.file_errors), i.file_errors


def test_fences_are_tracked_as_commonmark_tracks_them():
    """A ``` line inside another fence is that fence's text, and a longer or
    tilde fence is closed only by its own kind (#3416 review)."""
    md = ("````markdown\n```yaml\nhidden: 1\n```\n````\n"
          "```yaml\na: 1\n````\n"                  # ```` closes ``` (longer is fine)
          "~~~text\n```yaml\n~~~\n"
          "```yaml\nb: 2\n```\n")
    got = [(f.line, f.body) for f in inv.extract_fences("x.md", md)]
    assert got == [(6, ["a: 1"]), (12, ["b: 2"])]


@pytest.mark.parametrize("blank", ["\u3000", "\x1c", "\u00a0"])
def test_only_ascii_blanks_are_trimmed_from_a_fence_line(blank):
    """#3416 review: `str.strip()` also takes U+3000 / \\x1c away, Go's side
    does not — the two extractors would cut different units. Both trim only
    ' \\t\\r'; a line starting with another blank is no fence."""
    md = f"{blank}```yaml\na: 1\n```\n"
    assert list(inv.extract_fences("x.md", md)) == []


def test_crlf_and_a_lone_cr_keep_go_lines(tmp_path):
    """#3416 review (CodeRabbit): the file is read as bytes, so a lone CR is
    no line break and a CRLF's CR stays in the body line — as Go's
    os.ReadFile + strings.Split("\\n") sees it; line numbers agree."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_bytes(
        b"prose\rmore prose\r\n\r\n```yaml\r\na: 1\r\nb: 2\r\n```\r\n")
    (rel, text), = inv.iter_markdown(tmp_path)
    f, = inv.extract_fences(rel, text)
    assert (f.line, f.body) == (3, ["a: 1\r", "b: 2\r"])


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


@pytest.mark.parametrize("body, needle", [
    # conf.d text with a repeated key, declared .k8s to get past parse-tree:
    # kubectl keeps the last key, so only the object check stops it
    ("tenants:\n  t:\n    a: \"1\"\n    a: \"2\"\n", "not Kubernetes objects"),
    ("apiVersion: v1\nkind: ConfigMap\n---\n- a\n", "document(s) [2]"),
    ("# only a comment\n", "finds no document"),
])
def test_a_k8s_unit_must_hold_kubernetes_objects(tmp_path, body, needle):
    u, = inv.build(_docs(tmp_path, "```yaml {.k8s}\n" + body + "```\n")).units
    assert any(needle in e for e in u.errors), u.errors


def test_a_repeated_key_in_a_k8s_object_is_kubectls_to_answer(tmp_path):
    """Kept on purpose: kubectl reads a repeated key as the last one, so a
    declared manifest with one is read, not refused."""
    u, = inv.build(_docs(tmp_path, "```yaml {.k8s}\napiVersion: v1\nkind: A\nkind: B\n```\n")).units
    assert u.errors == [] and u.documents == [{"apiVersion": "v1", "kind": "B"}]


def test_each_expect_invalid_unit_must_be_invalid(tmp_path):
    """#3416 review: judged per fence, a counter-example unit (B1) hid every
    violation of a second, really broken unit (B2: a receiver that is a
    number). Per unit, B2 has violations of its own and passes — and a
    correct unit in such a fence is named."""
    b1 = "# a/_defaults.yaml\ndefaults:\n  foo: 1\nbogus_top: 1\n"
    b2 = "# b/t.yaml\ntenants:\n  tenant-x:\n    _routing:\n      receiver: 5\n"
    ok = "# c/t.yaml\ntenants:\n  tenant-x:\n    mysql_connections: \"70\"\n"
    root = _docs(tmp_path, "```yaml {.confd .expect-invalid}\n" + b1 + b2 + ok + "```\n")
    (tmp_path / "docs" / "schemas").mkdir()
    for name in ("tenant-config.schema.json", "platform-defaults.schema.json"):
        shutil.copy(REPO / "docs" / "schemas" / name, tmp_path / "docs" / "schemas" / name)
    r = subprocess.run(  # noqa: S603
        [sys.executable, "-X", "utf8", str(DRIFT), "--ci", "--repo-root", str(root)],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 1, r.stdout + r.stderr
    assert r.stdout.count("[SCHEMA]") == 1, r.stdout
    assert "[SCHEMA] docs/x.md:11 (expect-invalid)" in r.stdout, r.stdout
    assert "'# c/t.yaml'" in r.stdout, r.stdout
    assert "docs/x.md:2 — " in r.stdout and "docs/x.md:6 — " in r.stdout, r.stdout


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


# ── file headers ─────────────────────────────────────────────────────────────

def test_a_comment_that_mentions_a_file_does_not_cut(tmp_path):
    """#3416 review (F4): `# see conf.d/_defaults.yaml` used to cut the block,
    and a key repeated across the cut was in two units, read by nobody as
    one file. A header is a comment that starts with the file name; this one
    is prose, the block is one file, and the repeated key is refused."""
    body = ('```yaml\ntenants:\n  tenant-x:\n    pg_connections: "30"\n'
            "# 見 conf.d/_defaults.yaml\n"
            '    pg_connections: "300"\n```\n')
    u, = inv.build(_docs(tmp_path, body)).units
    assert u.line == 2 and "duplicate_key" in u.errors[0], u.errors
    r = _fences(tmp_path)
    assert r.returncode == 1 and "[FENCE] docs/x.md:2" in r.stdout, r.stdout


def test_a_header_with_a_trailing_note_still_cuts(tmp_path):
    body = ("```yaml\n# conf.d/_defaults.yaml (central)\ndefaults:\n  a: 1\n"
            "# conf.d/db-a.yaml — edge tenant\ntenants:\n  tenant-x: {}\n```\n")
    units = inv.build(_docs(tmp_path, body)).units
    assert [(u.line, u.errors) for u in units] == [(2, []), (5, [])]


# ── Go parity ────────────────────────────────────────────────────────────────

@pytest.mark.timeout(960)  # CI runs pytest with --timeout=300; the go build + test can take longer
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
         "^(TestDocsYAMLInventoryParity|TestExtractYAMLBlocks.*|"
         "TestDocsDefaults.*|TestDocsYamlSplit.*)$", "./pkg/config/"],
        cwd=REPO / "components" / "threshold-exporter" / "app",
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=900, env=env, check=False)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
