"""Tests for check_portal_asset_shipping.py — runtime assets must ship in the image.

Three duties, matching the three ways this gate can fail its users:

  * DETECTION — an asset loaded at runtime but absent from the Dockerfile's
    COPY allowlist is reported (the defect that motivated the lint: three
    already-shipped assets were missing).
  * NO FALSE POSITIVES — whole-directory COPYs really do cover their contents,
    API/CDN URLs are not assets, and navigation hrefs are not resource loads.
    A false red here blocks innocent PRs, which is how gates get disabled.
  * FAIL-CLOSED — a Dockerfile that cannot be parsed, or an empty scan corpus,
    must raise rather than pass vacuously.

Detector behaviour is exercised against synthetic trees; `TestLiveRepoIsClean`
pins the real tree separately, and `test_live_repo_would_catch_a_regression`
proves that pass is not vacuous by re-deleting a COPY line in a temp copy of
the REAL Dockerfile.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts" / "tools" / "lint"))

import check_portal_asset_shipping as gate  # noqa: E402


# ── fixture builders ────────────────────────────────────────────────

_DOCKERFILE = """\
FROM nginx:1.30-alpine3.23
COPY --chown=nginx:nginx docs/interactive/ /usr/share/nginx/html/interactive/
COPY --chown=nginx:nginx docs/assets/dist/ /usr/share/nginx/html/assets/dist/
COPY --chown=nginx:nginx docs/assets/jsx-loader.html /usr/share/nginx/html/assets/jsx-loader.html
COPY --chown=nginx:nginx docs/assets/platform-data.json /usr/share/nginx/html/assets/platform-data.json
"""


def _tree(root: Path, *, dockerfile: str = _DOCKERFILE, sources: dict | None = None,
          assets: tuple = ("platform-data.json", "template-data.json")) -> Path:
    """Materialise a miniature repo with the layout `audit()` expects."""
    (root / "components" / "da-portal").mkdir(parents=True)
    (root / "components" / "da-portal" / "Dockerfile").write_text(
        dockerfile, encoding="utf-8"
    )
    (root / "docs" / "assets" / "dist").mkdir(parents=True)
    (root / "docs" / "interactive").mkdir(parents=True)
    for name in assets:
        (root / "docs" / "assets" / name).write_text("{}", encoding="utf-8")
    for rel, body in (sources or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root


_TOOL = "tools/portal/src/interactive/tools/demo.jsx"


class TestDetection:
    def test_unshipped_asset_is_reported(self, tmp_path):
        root = _tree(tmp_path, sources={_TOOL: "fetch('template-data.json')\n"})
        violations, _ = gate.audit(root)
        assert len(violations) == 1
        assert "/assets/template-data.json" in violations[0]
        assert _TOOL in violations[0]

    def test_shipped_asset_is_clean(self, tmp_path):
        root = _tree(tmp_path, sources={_TOOL: "fetch('platform-data.json')\n"})
        assert gate.audit(root)[0] == []

    def test_removing_a_copy_line_turns_it_red(self, tmp_path):
        """The counterfactual: same source, one COPY line deleted."""
        stripped = "\n".join(
            line for line in _DOCKERFILE.splitlines()
            if "platform-data.json" not in line
        ) + "\n"
        root = _tree(tmp_path, dockerfile=stripped,
                     sources={_TOOL: "fetch('platform-data.json')\n"})
        violations, _ = gate.audit(root)
        assert any("/assets/platform-data.json" in v for v in violations)

    def test_hub_html_relative_path_resolves_to_assets(self, tmp_path):
        """`../assets/x.json` from /interactive/ is the same URL as `x.json`
        from /assets/ — the resolution bug this gate must not have."""
        root = _tree(tmp_path, sources={
            "docs/interactive/index.html":
                "<script>fetch('../assets/template-data.json')</script>",
        })
        violations, _ = gate.audit(root)
        assert len(violations) == 1
        assert "/assets/template-data.json" in violations[0]

    def test_stylesheet_link_tag_is_a_load_site(self, tmp_path):
        root = _tree(tmp_path, assets=("tokens.css",), sources={
            "docs/assets/shell.html": '<link rel="stylesheet" href="tokens.css">',
        })
        violations, _ = gate.audit(root)
        assert len(violations) == 1
        assert "/assets/tokens.css" in violations[0]

    @pytest.mark.parametrize("markup", [
        pytest.param('<link rel="stylesheet" href="tokens.css">', id="link-double"),
        pytest.param("<link rel='stylesheet' href='tokens.css'>", id="link-single"),
        pytest.param('<script src="tokens.css"></script>', id="script-double"),
        pytest.param("<script src='tokens.css'></script>", id="script-single"),
        pytest.param('<img src="tokens.css">', id="img-double"),
        pytest.param("<img src='tokens.css'>", id="img-single"),
        pytest.param("<img src=tokens.css>", id="img-unquoted"),
    ])
    def test_tag_attribute_quoting_does_not_disarm_the_gate(self, tmp_path, markup):
        """Regression: `_TAG_RE` once accepted ONLY double quotes, so the very
        same `<link>` in the very same file went unseen when written with
        single quotes — an undeclared fail-open, not a documented boundary."""
        root = _tree(tmp_path, assets=("tokens.css",),
                     sources={"docs/assets/shell.html": markup})
        violations, _ = gate.audit(root)
        assert len(violations) == 1, f"{markup!r} yielded {violations}"
        assert "/assets/tokens.css" in violations[0]

    def test_bare_assets_base_without_trailing_slash_is_seen(self, tmp_path):
        """Regression: a site resolving to exactly `/assets` failed
        `startswith('/assets/')` and was dropped entirely — no violation AND no
        directory requirement. The two spellings below are the same intent, and
        the one that used to be missed is the more natural one."""
        root = _tree(tmp_path, sources={
            _TOOL: "const ASSET_BASE = '/assets';\n"
                   "fetch(`${ASSET_BASE}/new-data.json`);\n",
        })
        violations, _ = gate.audit(root)
        assert len(violations) == 1, violations
        assert "/assets/ (directory)" in violations[0]

    def test_both_assets_base_spellings_agree(self, tmp_path):
        """`'/assets'` + `/x` and `'/assets/'` + `x` must resolve identically —
        where the slash sits must not decide whether the gate works."""
        without = gate.extract_references(
            _TOOL, "const B = '/assets';\nfetch(`${B}/new-data.json`);\n")
        with_ = gate.extract_references(
            _TOOL, "const B = '/assets/';\nfetch(`${B}new-data.json`);\n")
        assert [u for _, u in without] == [u for _, u in with_] == ["/assets/"]

    def test_copy_to_the_wrong_url_does_not_count_as_shipped(self, tmp_path):
        """Copied into the image, but not at the URL the browser requests."""
        misplaced = _DOCKERFILE.replace(
            "/usr/share/nginx/html/assets/platform-data.json",
            "/usr/share/nginx/html/data/platform-data.json",
        )
        root = _tree(tmp_path, dockerfile=misplaced,
                     sources={_TOOL: "fetch('platform-data.json')\n"})
        assert gate.audit(root)[0] != []


class TestNoFalsePositives:
    def test_directory_copy_covers_its_contents(self, tmp_path):
        """`COPY docs/assets/dist/ …` must satisfy a dynamic `dist/` path."""
        root = _tree(tmp_path, sources={
            "docs/assets/jsx-loader.html":
                "<script>var d = '../assets/dist/' + name + '.js';"
                " script.src = d;</script>",
        })
        assert gate.audit(root)[0] == []

    def test_directory_copy_covers_a_named_file_under_it(self, tmp_path):
        (tmp_path / "docs").mkdir()
        root = _tree(tmp_path, sources={_TOOL: "fetch('dist/demo.js')\n"})
        (root / "docs" / "assets" / "dist" / "demo.js").write_text("//", encoding="utf-8")
        assert gate.audit(root)[0] == []

    @pytest.mark.parametrize("expr", [
        "'/api/v1/tenants'",
        "'/preview'",
        "`/api/v1/tenants/${id}/metrics`",
        "'https://cdn.example.com/react.js'",
    ])
    def test_non_asset_urls_are_ignored(self, tmp_path, expr):
        root = _tree(tmp_path, sources={_TOOL: f"fetch({expr})\n"})
        assert gate.audit(root)[0] == []

    def test_navigation_href_is_not_a_load_site(self, tmp_path):
        """`window.location.href = '../assets/x.html?' + q` is navigation; a
        dynamic tail there must not demand that `/assets/` be COPYed wholesale."""
        root = _tree(tmp_path, sources={
            "docs/interactive/index.html":
                "<script>window.location.href = "
                "'../assets/jsx-loader.html?flow=' + sel;</script>",
        })
        assert gate.audit(root)[0] == []

    def test_dangling_reference_is_out_of_scope(self, tmp_path):
        """A path with no file behind it is a different defect — not this gate's."""
        root = _tree(tmp_path, sources={_TOOL: "fetch('does-not-exist.json')\n"})
        assert gate.audit(root)[0] == []

    @pytest.mark.parametrize("markup", [
        pytest.param('<a href="tokens.css">x</a>', id="anchor-double"),
        pytest.param("<a href='tokens.css'>x</a>", id="anchor-single"),
    ])
    def test_anchor_href_is_navigation_in_every_quoting(self, tmp_path, markup):
        """Widening the quote set must not widen the TAG set."""
        root = _tree(tmp_path, assets=("tokens.css",),
                     sources={"docs/assets/shell.html": markup})
        assert gate.audit(root)[0] == []

    def test_js_concatenated_tag_still_does_not_match(self, tmp_path):
        """The unquoted alternative must not start matching a tag being built
        in JS — the char after `src=` is a quote, which it excludes."""
        root = _tree(tmp_path, assets=("tokens.css",), sources={
            _TOOL: "el.innerHTML = '<img src=' + userPath + '>';\n",
        })
        assert gate.audit(root)[0] == []

    @pytest.mark.parametrize("expr", [
        "'/assetsx'",
        "`${B}/x.json`\nconst B = '/assetsx';",
    ])
    def test_assets_prefix_lookalike_is_not_an_asset(self, tmp_path, expr):
        """`/assets` normalisation is exact-match, so `/assetsx` stays out."""
        root = _tree(tmp_path, sources={_TOOL: f"fetch({expr})\n"})
        assert gate.audit(root)[0] == []

    def test_identifier_is_traced_to_its_literal(self, tmp_path):
        root = _tree(tmp_path, sources={
            _TOOL: "const ENDPOINT = '/api/v1/views';\nfetch(ENDPOINT);\n",
        })
        assert gate.audit(root)[0] == []


class TestFailClosed:
    def test_untraceable_expression_is_a_violation(self, tmp_path):
        root = _tree(tmp_path, sources={
            _TOOL: "async function go(u) { return fetch(u); }\n",
        })
        violations, _ = gate.audit(root)
        assert len(violations) == 1
        assert "cannot statically resolve" in violations[0]
        assert "EXEMPTIONS" in violations[0]

    def test_exemption_silences_only_its_own_key(self, tmp_path, monkeypatch):
        monkeypatch.setitem(gate.EXEMPTIONS, f"{_TOOL}::u", "test fixture")
        root = _tree(tmp_path, sources={
            _TOOL: "async function go(u) { return fetch(u); }\n",
        })
        violations, used = gate.audit(root)
        assert violations == []
        assert f"{_TOOL}::u" in used

    def test_missing_dockerfile_raises(self, tmp_path):
        (tmp_path / "docs").mkdir()
        with pytest.raises(gate.DockerfileParseError):
            gate.audit(tmp_path)

    def test_dockerfile_without_copies_raises(self, tmp_path):
        root = _tree(tmp_path, dockerfile="FROM nginx:1.30-alpine3.23\n",
                     sources={_TOOL: "fetch('platform-data.json')\n"})
        with pytest.raises(gate.DockerfileParseError):
            gate.audit(root)

    def test_malformed_copy_raises(self, tmp_path):
        with pytest.raises(gate.DockerfileParseError):
            gate.parse_copies("FROM nginx\nCOPY docs/assets/only-one-token\n")

    def test_empty_scan_corpus_raises(self, tmp_path):
        root = _tree(tmp_path, sources={})
        with pytest.raises(gate.DockerfileParseError):
            gate.audit(root)

    def test_line_continuation_copy_is_parsed(self, tmp_path):
        copies = gate.parse_copies(
            "FROM nginx\n"
            "COPY --chown=nginx:nginx \\\n"
            "    docs/assets/a.json \\\n"
            "    /usr/share/nginx/html/assets/a.json\n"
        )
        assert copies == [(("docs/assets/a.json",),
                           "/usr/share/nginx/html/assets/a.json")]


class TestLiveRepoIsClean:
    """The real tree must pass — this is the gate's own dogfood."""

    def test_live_repo_passes(self):
        violations, used = gate.audit(gate.REPO_ROOT)
        assert violations == [], "\n".join(violations)
        assert set(gate.EXEMPTIONS) == used, (
            "EXEMPTIONS entries that match no load site are stale: "
            f"{sorted(set(gate.EXEMPTIONS) - used)}"
        )

    def test_main_exits_zero(self):
        assert gate.main() == gate.EXIT_OK

    def test_live_repo_would_catch_a_regression(self, tmp_path):
        """Vacuous-pass protection against the REAL Dockerfile.

        If the extractor silently stopped finding load sites, the clean run
        above would pass forever. Re-introduce the exact defect this lint was
        written for — delete the recommendation-data.json COPY — in a temp
        copy of the real Dockerfile, and require the gate to notice.
        """
        real = (gate.REPO_ROOT / "components" / "da-portal" / "Dockerfile")
        text = real.read_text(encoding="utf-8")
        assert "recommendation-data.json" in text, "fix landed differently"
        stripped = "\n".join(
            line for line in text.splitlines()
            if "recommendation-data.json" not in line
        ) + "\n"

        broken = tmp_path / "Dockerfile"
        broken.write_text(stripped, encoding="utf-8")
        copies = gate.parse_copies(broken.read_text(encoding="utf-8"))
        assert not gate.is_shipped(copies, "/assets/recommendation-data.json")
        # …and the unmodified one does ship it (both directions asserted).
        assert gate.is_shipped(gate.parse_copies(text),
                               "/assets/recommendation-data.json")

    def test_known_load_sites_are_still_seen(self):
        """Pin the references we KNOW exist, so a broken regex fails here
        instead of making the clean run vacuous."""
        expected = {
            "tools/portal/src/interactive/tools/AlertPreviewTab.jsx":
                "/assets/recommendation-data.json",
            "tools/portal/src/interactive/tools/template-gallery.jsx":
                "/assets/template-data.json",
            "docs/assets/jsx-loader.html": "/assets/design-tokens.css",
            "docs/interactive/index.html": "/assets/tool-registry.yaml",
        }
        for rel, url in expected.items():
            path = gate.REPO_ROOT / rel
            urls = {
                u for _, u in
                gate.extract_references(rel, path.read_text(encoding="utf-8"))
            }
            assert url in urls, f"{rel} no longer yields {url}: saw {sorted(urls)}"

    def test_dist_directory_reference_is_seen_and_covered(self):
        """The one live dynamic-prefix path: `'../assets/dist/' + name + '.js'`
        assigned to `script.src` must resolve to the DIRECTORY requirement."""
        rel = "docs/assets/jsx-loader.html"
        text = (gate.REPO_ROOT / rel).read_text(encoding="utf-8")
        urls = {u for _, u in gate.extract_references(rel, text)}
        assert "/assets/dist/" in urls
        copies = gate.parse_copies(
            (gate.REPO_ROOT / "components" / "da-portal" / "Dockerfile")
            .read_text(encoding="utf-8")
        )
        assert gate.is_shipped(copies, "/assets/dist/")
