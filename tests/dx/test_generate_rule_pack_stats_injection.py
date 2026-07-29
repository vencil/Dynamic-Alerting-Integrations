"""Contract tests for the sentinel injections in generate_rule_pack_stats.py.

The generator writes `docs/includes/rule-pack-stats.{md,en.md}` (a fragment that
had no consumer for its whole life, #1267) AND injects two tables in place:

* `docs/architecture-and-design.{md,en.md}` — the stats table (#1267)
* `docs/design/rule-packs.{md,en.md}` — the §3.1 inventory table (#1277)

Both injections use sentinels rather than `--8<--` snippets on purpose: those
docs are read on github.com, where pymdownx.snippets does not run and a snippet
directive renders as a literal line with no table under it.

What is pinned here, and why each one:

* **idempotency** — the end sentinel must not absorb a newline. The first draft
  of this mechanism let it, so every run added a blank line and `--check` could
  never converge. Burned once already.
* **the inventory table has no ownership column** — #1277's whole point. Its
  per-pack mapping had no machine-readable source, contradicted
  `.github/CODEOWNERS` at the time, and had not changed in 3.7 months while
  the counts changed repeatedly. Ownership lives in CODEOWNERS; the
  illustrative split lives in a clearly-labelled block *outside* the
  generated table.
* **column-count consistency** — a regex-based hand-fix once produced a total
  row with 4 cells against a 5-cell header, and it shipped because the fixer
  trusted its own bookkeeping instead of reading the rendered markdown.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "tools" / "dx" / "generate_rule_pack_stats.py"

sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "dx"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))

import generate_rule_pack_stats as gen  # noqa: E402

_TIMEOUT = 120
_LANGS = ("zh", "en")


@pytest.fixture(scope="module")
def stats():
    return gen.gather_stats()


# --------------------------------------------------------------------------
# sentinel mechanics
# --------------------------------------------------------------------------
@pytest.mark.parametrize("start,end", [
    (gen.ARCH_SENTINEL_START, gen.ARCH_SENTINEL_END),
    (gen.DESIGN_SENTINEL_START, gen.DESIGN_SENTINEL_END),
])
def test_block_replacement_is_idempotent(start, end):
    empty = f"before\n{start}\n{end}\nafter\n"
    body = "| a |\n|---|\n| 1 |\n"
    once = gen._replace_block(empty, body, "docs/x.md", start, end)
    twice = gen._replace_block(once, body, "docs/x.md", start, end)
    assert once == twice, "sentinel replacement is not idempotent"
    assert "before" in once and "after" in once


def test_missing_sentinel_raises():
    with pytest.raises(ValueError, match="sentinel"):
        gen._replace_block("nothing here\n", "| t |\n", "docs/x.md",
                           gen.DESIGN_SENTINEL_START, gen.DESIGN_SENTINEL_END)


# --------------------------------------------------------------------------
# the inventory table (#1277)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("lang", _LANGS)
def test_inventory_table_has_no_ownership_column(stats, lang):
    """#1277: the fabricated team vocabulary must not come back."""
    table = gen.render_design_table(stats, lang=lang)
    header = table.split("\n")[0]
    assert "擁有團隊" not in header and "Owning Team" not in header
    # Deliberately NOT "Messaging"/"Analytics": those are legitimate pack
    # *category* display names with a machine SoT (generate_platform_data.
    # PACK_CATEGORY), so banning them would misfire the day a category column
    # is added. Only the fabricated ownership vocabulary is forbidden.
    for word in ("DBA", "AppData", "AppDev"):
        assert word not in table, f"{word!r} leaked back into the generated table"


@pytest.mark.parametrize("lang", _LANGS)
def test_inventory_table_column_counts_are_uniform(stats, lang):
    """A hand-fix once shipped a 4-cell total row under a 5-cell header."""
    rows = [r for r in gen.render_design_table(stats, lang=lang).split("\n") if r.startswith("|")]
    widths = {r.count("|") for r in rows}
    assert len(widths) == 1, f"ragged table: column counts {widths}"


@pytest.mark.parametrize("lang", _LANGS)
def test_inventory_table_covers_every_pack_exactly_once(stats, lang):
    table = gen.render_design_table(stats, lang=lang)
    found = re.findall(r"`prometheus-rules-([a-z0-9]+)`", table)
    assert sorted(found) == sorted(gen._pack_order())
    assert len(found) == len(set(found)) == 16


@pytest.mark.parametrize("lang", _LANGS)
def test_inventory_totals_match_the_rows(stats, lang):
    """The total row must be the sum of the rendered rows, not a separate claim."""
    table = gen.render_design_table(stats, lang=lang)
    rec = sum(int(m[0]) for m in re.findall(r"\| (\d+) \| (\d+) \|$", table, re.M))
    alert = sum(int(m[1]) for m in re.findall(r"\| (\d+) \| (\d+) \|$", table, re.M))
    assert rec == stats["recording"] and alert == stats["alert"]
    assert f"**{stats['recording']}**" in table and f"**{stats['alert']}**" in table
    assert f"**{stats['total']}**" in table


def test_labels_come_from_the_shared_source_not_a_local_copy():
    """Re-typing the labels here would create the 7th hand-maintained copy."""
    from generate_platform_data import PACK_LABELS, PACK_ORDER
    assert gen._pack_order() == list(PACK_ORDER)
    for key in PACK_ORDER:
        assert gen._pack_label(key) == PACK_LABELS.get(key, key)


# --------------------------------------------------------------------------
# shipped tree
# --------------------------------------------------------------------------
def test_check_is_green_on_shipped_tree():
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--check", "--lang", "all"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), timeout=_TIMEOUT,
    )
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("lang", _LANGS)
def test_shipped_design_doc_keeps_ownership_outside_the_generated_block(lang):
    """The illustrative split must survive, but strictly outside the sentinels."""
    doc = gen._design_doc(lang).read_text(encoding="utf-8")
    block = doc.split(gen.DESIGN_SENTINEL_START)[1].split(gen.DESIGN_SENTINEL_END)[0]
    assert "CODEOWNERS" not in block, "the generated block must stay pure inventory"
    marker = "示意" if lang == "zh" else "illustrative"
    assert marker in doc, "the illustrative team-split section went missing"
    assert "CODEOWNERS" in doc, "the doc must point at the real ownership source"
