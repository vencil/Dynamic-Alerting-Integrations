"""Tests for sync_tool_registry.py — tool-registry.yaml → Hub + CUSTOM_FLOW_MAP sync.

Regression coverage for two bugs found while landing PR #763 (S6b-1 recipe-builder):

  1. sync_hub_cards BLANKED every card's `data-audience` (set it to "") instead
     of populating it from the registry `audience:` list. Root cause: the script's
     own parse_registry wrote ``current[key] = ""`` for the empty scalar that
     precedes a block list, shadowing the later `- item` append → audience parsed
     as "". The fix (skip empty scalars) mirrors lint_tool_consistency.py.

  2. sync_tool_meta targeted a long-dead ``var TOOL_META = {...}`` block (removed in
     the ESM dist-bundle migration). Its regex no longer matched jsx-loader.html,
     whose live key→path map is the flat ``var CUSTOM_FLOW_MAP = {...}`` object, so
     the sync path always errored and was a no-op. The fix retargets CUSTOM_FLOW_MAP.
"""
from __future__ import annotations

import os
import re
import sys
import textwrap

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx")
sys.path.insert(0, _TOOLS_DIR)

import sync_tool_registry as srt  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
# A registry whose list/dict fields use BLOCK syntax (the format the real
# tool-registry.yaml uses) — this is what tripped the empty-scalar parser bug.
# `alpha`/`beta` already have cards in the fixture hub; `gamma` is new (missing).
_REGISTRY_YAML = textwrap.dedent("""\
    tools:
    - key: alpha
      title:
        en: Alpha Tool
        zh: 甲工具
      file: interactive/tools/alpha.jsx
      audience:
      - tenant
      - platform
    - key: beta
      title:
        en: Beta Tool
        zh: 乙工具
      file: interactive/tools/beta.jsx
      audience:
      - platform
      - domain
    - key: gamma
      title:
        en: Gamma Tool
        zh: 丙工具
      file: interactive/tools/gamma.jsx
      audience:
      - sre
""")

# Hub fixture: `alpha` has an unsorted audience, `beta` has a BLANK audience
# (simulating the post-bug corrupted state). `gamma` is absent entirely.
_HUB_HTML = textwrap.dedent("""\
    <!DOCTYPE html>
    <html><body>
    <div style="display:none;" id="linter-cards">
      <!-- alpha (interactive/tools/alpha.jsx) -->
      <a class="card" data-audience="tenant,platform" href="interactive/tools/alpha.jsx">Alpha Tool</a>
      <!-- beta (interactive/tools/beta.jsx) -->
      <a class="card" data-audience="" href="interactive/tools/beta.jsx">Beta Tool</a>
    </div>
    </body></html>
""")

# Loader fixture: CUSTOM_FLOW_MAP missing the `gamma` entry.
_LOADER_HTML = textwrap.dedent("""\
    <!DOCTYPE html>
    <html><body>
    <script>
    (function() {
      var CUSTOM_FLOW_MAP = {
        'alpha': '../interactive/tools/alpha.jsx',
        'beta': '../interactive/tools/beta.jsx'
      };
    })();
    </script>
    </body></html>
""")


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """Write the three fixture files and point the module globals at them."""
    reg = tmp_path / "tool-registry.yaml"
    hub = tmp_path / "index.html"
    loader = tmp_path / "jsx-loader.html"
    reg.write_text(_REGISTRY_YAML, encoding="utf-8")
    hub.write_text(_HUB_HTML, encoding="utf-8")
    loader.write_text(_LOADER_HTML, encoding="utf-8")
    monkeypatch.setattr(srt, "REGISTRY_PATH", reg)
    monkeypatch.setattr(srt, "HUB_PATH", hub)
    monkeypatch.setattr(srt, "LOADER_PATH", loader)
    return reg, hub, loader


# ---------------------------------------------------------------------------
# parse_registry — the root-cause regression
# ---------------------------------------------------------------------------
class TestParseRegistry:
    def test_block_list_audience_parses_to_list(self, tmp_path):
        """Regression: block-list `audience:` must parse to a list, not ""."""
        reg = tmp_path / "registry.yaml"
        reg.write_text(_REGISTRY_YAML, encoding="utf-8")
        tools = srt.parse_registry(str(reg))
        by_key = {t["key"]: t for t in tools}
        assert by_key["alpha"]["audience"] == ["tenant", "platform"]
        assert by_key["beta"]["audience"] == ["platform", "domain"]
        assert by_key["gamma"]["audience"] == ["sre"]
        # The pre-fix bug produced "" (empty string) for every block-list field.
        assert by_key["alpha"]["audience"] != ""

    def test_nested_block_dict_title(self, tmp_path):
        """Block-form `title:` with en:/zh: children parses to a dict (not "")."""
        reg = tmp_path / "registry.yaml"
        reg.write_text(_REGISTRY_YAML, encoding="utf-8")
        by_key = {t["key"]: t for t in srt.parse_registry(str(reg))}
        assert by_key["alpha"]["title"] == {"en": "Alpha Tool", "zh": "甲工具"}

    def test_inline_list_and_dict_grammar(self, tmp_path):
        """Grammar-space (not in the real corpus): inline `[a, b]` / `{ en: x }`
        must parse the same as their block forms. The real registry only uses
        block syntax, but the parser admits inline — lock it so a future inline
        edit doesn't silently mis-parse."""
        reg = tmp_path / "registry.yaml"
        reg.write_text(textwrap.dedent("""\
            tools:
            - key: inline-tool
              title: { en: "Inline Tool", zh: "內聯" }
              file: interactive/tools/inline.jsx
              audience: [platform, tenant]
              tags: [a, b]
        """), encoding="utf-8")
        t = srt.parse_registry(str(reg))[0]
        assert t["title"] == {"en": "Inline Tool", "zh": "內聯"}
        assert t["audience"] == ["platform", "tenant"]
        assert t["tags"] == ["a", "b"]

    def test_block_list_strips_quotes(self, tmp_path):
        """Quoted block-list items get surrounding quotes stripped — parity with
        the inline-list / dict / scalar paths (CodeRabbit #764)."""
        reg = tmp_path / "registry.yaml"
        reg.write_text(textwrap.dedent('''\
            tools:
            - key: q
              file: interactive/tools/q.jsx
              audience:
              - "platform"
              - 'tenant'
        '''), encoding="utf-8")
        t = srt.parse_registry(str(reg))[0]
        assert t["audience"] == ["platform", "tenant"]

    def test_sibling_field_after_block_resets(self, tmp_path):
        """A field at the same indent as a block field must NOT be swallowed
        into that block (pending-block must reset). Guards the indentation
        bookkeeping that distinguishes nested dict entries from siblings."""
        reg = tmp_path / "registry.yaml"
        reg.write_text(textwrap.dedent("""\
            tools:
            - key: t
              audience:
              - platform
              icon: rules
              file: interactive/tools/t.jsx
        """), encoding="utf-8")
        t = srt.parse_registry(str(reg))[0]
        assert t["audience"] == ["platform"]
        assert t["icon"] == "rules"            # sibling, not an audience item
        assert t["file"] == "interactive/tools/t.jsx"
        assert "rules" not in t["audience"]


def _apply(step, tools, **kw):
    """Run one sync step and write what it left pending, as `main` does."""
    pending: list = []
    result = step(tools, pending=pending, **kw)
    srt.write_pending(pending)
    return result


# ---------------------------------------------------------------------------
# sync_hub_cards — (a) populates/normalizes audience, (b) inserts missing card
# ---------------------------------------------------------------------------
class TestSyncHubCards:
    def test_populates_and_sorts_audience_never_blanks(self, wired):
        _reg, hub, _loader = wired
        tools = srt.parse_registry(str(_reg))
        changed = _apply(srt.sync_hub_cards, tools, dry_run=False, verbose=False)
        assert changed is True
        out = hub.read_text(encoding="utf-8")

        # The headline bug: no card may end up with an empty audience.
        assert 'data-audience=""' not in out
        # alpha: unsorted "tenant,platform" → sorted "platform,tenant".
        assert (
            '<a class="card" data-audience="platform,tenant" '
            'href="interactive/tools/alpha.jsx">' in out
        )
        # beta: blanked "" → populated + sorted "domain,platform".
        assert (
            '<a class="card" data-audience="domain,platform" '
            'href="interactive/tools/beta.jsx">' in out
        )

    def test_inserts_missing_card_without_touching_unrelated(self, wired):
        _reg, hub, _loader = wired
        tools = srt.parse_registry(str(_reg))
        _apply(srt.sync_hub_cards, tools, dry_run=False, verbose=False)
        out = hub.read_text(encoding="utf-8")

        # gamma was absent → inserted with its registry audience + title.
        assert (
            '<a class="card" data-audience="sre" '
            'href="interactive/tools/gamma.jsx">Gamma Tool</a>' in out
        )
        # Exactly one card per tool — insertion must not duplicate alpha/beta.
        assert out.count('href="interactive/tools/alpha.jsx"') == 1
        assert out.count('href="interactive/tools/beta.jsx"') == 1
        assert out.count('href="interactive/tools/gamma.jsx"') == 1
        # The new card lands INSIDE the #linter-cards block (before its </div>).
        block = out.split('id="linter-cards"', 1)[1].split("</div>", 1)[0]
        assert "interactive/tools/gamma.jsx" in block

    def test_idempotent(self, wired):
        """A second run after applying must report no further changes."""
        _reg, hub, _loader = wired
        tools = srt.parse_registry(str(_reg))
        assert _apply(srt.sync_hub_cards, tools, dry_run=False, verbose=False) is True
        assert _apply(srt.sync_hub_cards, tools, dry_run=False, verbose=False) is False

    def test_dry_run_does_not_write(self, wired):
        _reg, hub, _loader = wired
        before = hub.read_text(encoding="utf-8")
        tools = srt.parse_registry(str(_reg))
        assert _apply(srt.sync_hub_cards, tools, dry_run=True, verbose=False) is True
        assert hub.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# sync_tool_meta — retargeted onto CUSTOM_FLOW_MAP
# ---------------------------------------------------------------------------
class TestSyncFlowMap:
    def test_inserts_missing_map_entry(self, wired):
        _reg, _hub, loader = wired
        tools = srt.parse_registry(str(_reg))
        changed = _apply(srt.sync_tool_meta, tools, dry_run=False, verbose=False)
        assert changed is True
        out = loader.read_text(encoding="utf-8")
        assert "'gamma': '../interactive/tools/gamma.jsx'" in out
        # Block stays intact + every key present exactly once.
        assert "var CUSTOM_FLOW_MAP = {" in out
        for key in ("alpha", "beta", "gamma"):
            assert out.count(f"'{key}':") == 1

    def test_idempotent(self, wired):
        _reg, _hub, loader = wired
        tools = srt.parse_registry(str(_reg))
        assert _apply(srt.sync_tool_meta, tools, dry_run=False, verbose=False) is True
        assert _apply(srt.sync_tool_meta, tools, dry_run=False, verbose=False) is False

    def test_errors_when_block_absent(self, tmp_path, monkeypatch, capsys):
        """No CUSTOM_FLOW_MAP block → explicit error + returns None (fatal
        sentinel, distinct from the False no-op so callers fail loud)."""
        loader = tmp_path / "loader.html"
        loader.write_text("<html><body>no map here</body></html>", encoding="utf-8")
        monkeypatch.setattr(srt, "LOADER_PATH", loader)
        ok = _apply(srt.sync_tool_meta, [{"key": "x", "file": "x.jsx"}], dry_run=False, verbose=False)
        assert ok is None
        assert "Could not find CUSTOM_FLOW_MAP" in capsys.readouterr().err

    def test_main_exits_caller_error_when_flow_map_absent(self, wired, monkeypatch):
        """main() must propagate the fatal None as a non-zero caller-error exit,
        not swallow it and exit 0 (CodeRabbit #764)."""
        _reg, _hub, loader = wired
        loader.write_text("<html>no CUSTOM_FLOW_MAP</html>", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["sync_tool_registry.py"])
        with pytest.raises(SystemExit) as exc:
            srt.main()
        assert exc.value.code == srt.EXIT_CALLER_ERROR


class TestRegistryIsTheOnlyAudienceSource:
    """JSX frontmatter 不帶 audience/tags（issue #1454）。

    那份副本沒有讀取端（portal build 整段剝掉 frontmatter），用的詞彙又和
    registry 的封閉詞彙不同（`platform-engineer` 對 `platform`），於是它必然
    和 registry 分歧、而分歧沒有人看得到。registry 是唯一的來源；這條擋的是
    有人把副本加回去。
    """

    _FM = re.compile(r"\A---\n(.*?)\n---", re.S)
    _KEY = re.compile(r"^(audience|tags)\s*:", re.M)

    def test_no_portal_jsx_frontmatter_carries_audience_or_tags(self):
        root = srt.PROJECT_ROOT / "tools" / "portal" / "src"
        with_fm, offenders = 0, []
        for jsx in sorted(root.rglob("*.jsx")):
            m = self._FM.match(jsx.read_text(encoding="utf-8"))
            if not m:
                continue
            with_fm += 1
            keys = self._KEY.findall(m.group(1))
            if keys:
                offenders.append(f"{jsx.relative_to(root).as_posix()}: {keys}")
        # 前提：真的掃到了帶 frontmatter 的 JSX，否則「零違規」只是沒掃到。
        assert with_fm >= 40, f"only {with_fm} JSX with frontmatter under {root}"
        assert not offenders, (
            "JSX frontmatter must not duplicate the registry's audience/tags "
            f"(docs/assets/tool-registry.yaml is the only source): {offenders}")

# ---------------------------------------------------------------------------
# Pure generators
# ---------------------------------------------------------------------------
class TestGenerators:
    def test_generate_card_html_sorts_audience(self):
        tool = {
            "key": "gamma",
            "file": "interactive/tools/gamma.jsx",
            "audience": ["tenant", "domain", "platform"],
            "title": {"en": "Gamma Tool"},
        }
        html = srt.generate_card_html(tool)
        assert 'data-audience="domain,platform,tenant"' in html
        assert 'href="interactive/tools/gamma.jsx"' in html
        assert ">Gamma Tool</a>" in html

    def test_generate_flow_map_paths_and_trailing_comma(self):
        tools = [
            {"key": "a", "file": "interactive/tools/a.jsx"},
            {"key": "b", "file": "getting-started/b.jsx"},
        ]
        out = srt.generate_flow_map(tools)
        assert "'a': '../interactive/tools/a.jsx'," in out  # not-last → comma
        assert "'b': '../getting-started/b.jsx'" in out  # last → no comma
        assert not out.rstrip().rstrip("};").rstrip().endswith(",")


# ---------------------------------------------------------------------------
# #3981: both targets are computed and judged before the first write
# ---------------------------------------------------------------------------
_STDOUT_DRIVER = """\
import sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import sync_tool_registry as srt, _lib_exitcodes
d = Path(sys.argv[3])
srt.REGISTRY_PATH = d / "tool-registry.yaml"
srt.HUB_PATH = d / "index.html"
srt.LOADER_PATH = d / "jsx-loader.html"
sys.argv = ["sync_tool_registry.py"]
_lib_exitcodes.run(srt.main)
"""


def test_no_target_is_written_when_the_hub_is_this_process_stdout(wired, tmp_path):
    """stdout appended onto the hub page, the second target: rc 2, one line
    naming it, and the loader page, the first, keeps its bytes. The bare
    writes rewrote the loader and then the hub, the report landing in it."""
    import subprocess
    _reg, hub, loader = wired
    before = {p: p.read_bytes() for p in (hub, loader)}
    dx = os.path.abspath(_TOOLS_DIR)
    with open(hub, "a", encoding="utf-8") as out:
        proc = subprocess.run([sys.executable, "-c", _STDOUT_DRIVER, dx, os.path.dirname(dx),
                               str(tmp_path)],
                              stdout=out, stderr=subprocess.PIPE, text=True,
                              encoding="utf-8", timeout=60)
    assert proc.returncode == 2, proc.stderr
    assert proc.stderr.strip().splitlines() == [
        f"ERROR: cannot write {hub}: is this process's standard stream"
        " — internal output path, this is a bug or an unwritable workspace"], proc.stderr
    assert loader.read_bytes() == before[loader]
    # The report printed ahead of the writes lands in the hub (it is stdout);
    # its own text is untouched.
    assert hub.read_bytes().startswith(before[hub])


@pytest.mark.parametrize("shape", ["fifo", "not-utf8"])
def test_a_target_that_cannot_be_read_is_rc2_one_line(shape, wired, monkeypatch, capsys):
    """A FIFO where the loader page goes blocked the read forever; non-UTF-8
    text ended in the crash handler. Now rc 2, one line naming it."""
    import signal
    _reg, _hub, loader = wired
    loader.unlink()
    if shape == "fifo":
        if not hasattr(os, "mkfifo") or not hasattr(signal, "SIGALRM"):
            pytest.skip("no FIFO / SIGALRM on this platform")
        os.mkfifo(loader)
        why = "not a regular file"
    else:
        loader.write_bytes(b"\xff\xfe")
        why = "not UTF-8 (invalid start byte)"
    monkeypatch.setattr(sys, "argv", ["sync_tool_registry.py", "--dry-run"])

    def hung(signum, frame):
        raise AssertionError("blocked on the FIFO")
    prev = signal.signal(signal.SIGALRM, hung) if hasattr(signal, "SIGALRM") else None
    if prev is not None:
        signal.alarm(20)
    try:
        with pytest.raises(SystemExit) as exc:
            srt.main()
    finally:
        if prev is not None:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, prev)
    err = capsys.readouterr().err
    assert exc.value.code == 2, err
    assert err.strip().splitlines() == [
        f"ERROR: cannot read {loader}: {why}"
        " — internal path, this is a bug or an unreadable workspace"], err


def test_a_read_error_is_the_read_direction_error(wired, monkeypatch):
    """An OSError on the read (EACCES cannot be denied to root, so it is
    raised) is OutputWriteError(action="read"): rc 2 at `main`."""
    from pathlib import Path
    from _lib_output import OutputWriteError
    _reg, hub, _loader = wired

    def denied(self, *a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(OutputWriteError) as exc:
        srt._read(hub)
    assert exc.value.action == "read" and exc.value.path == str(hub)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_rewritten_page_gets_the_mode_a_checkout_would_give(wired):
    """Through `write_repo_artifact`: 0644 — the bare write kept 0640."""
    reg, hub, _loader = wired
    hub.chmod(0o640)
    assert _apply(srt.sync_hub_cards, srt.parse_registry(str(reg)),
                  dry_run=False, verbose=False) is True
    assert hub.stat().st_mode & 0o777 == 0o644


def test_each_page_reports_its_change_after_it_is_written(wired, monkeypatch, capsys):
    """The `[flow_map] Updated …` / `[hub] Applied …` lines are printed by
    `write_pending`, once that page is written — not by the step that
    computed it."""
    _reg, hub, loader = wired
    seen = []
    real = srt.write_repo_artifact

    def spy(path, text, **kw):
        seen.append(("write", path))
        real(path, text, **kw)
    monkeypatch.setattr(srt, "write_repo_artifact", spy)
    monkeypatch.setattr("builtins.print", lambda *a, **k: seen.append(("print", " ".join(map(str, a)))))
    monkeypatch.setattr(sys, "argv", ["sync_tool_registry.py"])
    with pytest.raises(SystemExit) as exc:
        srt.main()
    assert exc.value.code == 0
    lines = [x for kind, x in seen if kind == "print"]
    flow = seen.index(("print", "[flow_map] Updated CUSTOM_FLOW_MAP (3 entries)"))
    applied = next(i for i, (k, x) in enumerate(seen) if k == "print" and x.startswith("[hub] Applied "))
    assert seen.index(("write", loader)) < flow < seen.index(("write", hub)) < applied, seen
    assert any(x.startswith("  new card: gamma") for x in lines), lines
