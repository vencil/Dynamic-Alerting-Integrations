"""Editor schema binding ↔ CI schema gate: same file set (#1815, #1911).

`.devcontainer/devcontainer.json` binds conf.d files to two JSON Schemas via
`yaml.schemas`; `scripts/tools/lint/check_confd_schema.py` validates conf.d
files against the SAME two schemas. The devcontainer comment claims the two
agree. This test DERIVES that claim instead of trusting it:

  * the editor side is the glob list read out of devcontainer.json;
  * the CI side is `check_confd_schema.validate_dir` itself, run over a real
    temp tree with a recording validator — the gate's own walker and its own
    tenant / `_defaults` / skip classification, not a re-statement of them.

Over one corpus of conf.d-relative paths (flat + nested, every extension
spelling and case, `_defaults*`, other `_` files, hidden names, dot-dirs) the
set each schema's globs bind must equal the set CI sends to that schema —
except for a DECLARED difference per engine, which is pinned, not excluded.

TWO ENGINES, both modelled, both compared (the source read for each is
npm-packed `yaml-language-server` + `picomatch` 4.0.5, the version
vscode-yaml 1.24.0's lockfile bundles):

  * `bash`    — yaml-language-server 1.24.0: `picomatch(['**/' + p],
                {bash: true, noglobstar: false})`. A bare `*` is `.*?` and
                crosses `/` (parse.js: `token.output = '.*?'`), and a middle
                `/**/` carries no leading-dot guard after its first slash,
                so a dot-dir is entered when it is the FIRST directory under
                `conf.d/` and refused deeper down.
  * `default` — the unreleased `next` line, which drops `bash`: `*` is
                `[^/]*?`, and `**` never enters a dot-directory.

Both engines match the `file:///…` URI string yaml-language-server builds
(`normalizeResourceForMatching`), so the corpus is fed as that string.

⛔ THE MATCHER IS A MODEL, NOT THE ENGINE. No stdlib Python matcher agrees
with picomatch here — `pathlib.PurePath.full_match` reads `[^_]` as the
literal class {`^`, `_`} and has no extglob. `_glob_to_regex` reproduces the
regex picomatch 4.0.5 emits for the few constructs these globs use and
REFUSES everything else. `test_translator_agrees_with_real_picomatch` runs
the real engine in both modes and must agree cell for cell; CI's Python Tests
job installs the pinned picomatch and sets `VIBE_REQUIRE_PICOMATCH=1`, so
there that anchor fails instead of skipping.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "lint"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))

from check_confd_schema import validate_dir  # noqa: E402

DEVCONTAINER = REPO_ROOT / ".devcontainer" / "devcontainer.json"
TENANT_SCHEMA_KEY = "./docs/schemas/tenant-config.schema.json"
PLATFORM_SCHEMA_KEY = "./docs/schemas/platform-defaults.schema.json"
MODES = ("bash", "default")

# What yaml-language-server actually matches: the document's file:// URI.
_URI_PREFIX = "file:///workspaces/vibe-k8s-lab/components/threshold-exporter/config/conf.d/"
# A workspace that itself sits under a dot-directory (e.g. a git worktree in
# `.claude/worktrees/`): neither engine lets the leading `**` cross it.
_DOT_WORKSPACE_URI = "file:///home/user/repo/.claude/worktrees/wt/config/conf.d/db-a.yaml"

_NAMES = (
    "db-a.yaml", "db-a.yml", "db-a.YAML", "db-a.Yml", "x.yaml.yml",
    "_defaults.yaml", "_defaults.yml", "_defaults-multidb.yml",
    "_DEFAULTS.YAML", "_Defaults.Yml",
    "_other.yaml", "_routing_profiles.yml", "_.yaml",
    ".hidden.yaml", ".hidden.yml",
    "notes.txt", "db-a.yaml.bak", "yaml",
)
_DIRS = ("", "sub/", "a/b/", "_archive/", ".hid/", ".hid/sub/", "a/.hid/")
CORPUS = tuple(d + n for d in _DIRS for n in _NAMES)

# Must-fire controls: if the corpus/assembly silently binds nothing, these fail.
_MUST_TENANT = ("db-a.yaml", "db-a.yml", "sub/db-a.YAML", "a/b/db-a.Yml")
_MUST_PLATFORM = ("_defaults.yaml", "_defaults-multidb.yml", "sub/_DEFAULTS.YAML")


def _dot_dirs(rel: str) -> list[int]:
    """Indexes of the directory components of `rel` that start with `.`."""
    return [i for i, part in enumerate(rel.split("/")[:-1]) if part.startswith(".")]


# The declared difference from CI, per engine: paths CI validates (its
# os.walk enters every directory) that the editor binds to NO schema.
DECLARED_UNBOUND = {
    "bash": lambda rel: any(i > 0 for i in _dot_dirs(rel)),
    "default": lambda rel: bool(_dot_dirs(rel)),
}


# ---------------------------------------------------------------- JSONC
def _strip_jsonc(text: str) -> str:
    """Remove `//` and `/* */` comments OUTSIDE string literals.

    String-aware on purpose: devcontainer.json carries `https://...` inside
    string values, and a line-regex strip would cut those in half.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j == -1:
                raise ValueError("unterminated /* comment in JSONC")
            i = j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _load_yaml_schemas(text: str) -> dict:
    data = json.loads(_strip_jsonc(text))
    return data["customizations"]["vscode"]["settings"]["yaml.schemas"]


# ---------------------------------------------------------------- glob model
# picomatch 4.0.5 lib/constants.js + parse.js, POSIX:
_NO_DOT = r"(?!\.)"
_GLOBSTAR = r"(?:(?:(?!(?:^|/)\.).)*?)"      # `globstar(opts)`, dot: false
_UNSUPPORTED = set("?{}()!+@\\")


def _class(cls: str) -> str:
    inner = cls[1:-1]
    if not inner or any(ch in inner for ch in "[\\"):
        raise ValueError(f"character class {cls!r} not modelled")
    if inner[0] in "^!":
        return "[^" + inner[1:] + "/]"      # picomatch adds `/` to a negated class
    # picomatch emits `(?:\[yY\]|[yY])` — it also accepts the literal bracket
    # text. Modelled as the class alone: only a path containing a literal
    # `[yY]` would tell them apart, and no corpus entry does.
    return "[" + inner + "]"


def _segment_to_regex(seg: str, bash: bool) -> str:
    """One non-globstar path segment → the regex picomatch emits for it.

    Supported: literals, `*`, `[...]` / `[^...]` / `[!...]`, and the extglob
    `*([...])` when it is not the first token. Anything else raises — a model
    that guesses would be a second, unaudited opinion.
    """
    out: list[str] = []
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if seg.startswith("*([", i):
            if i == 0:
                raise ValueError(f"leading extglob not modelled ({seg!r})")
            j = seg.find("])", i + 3)
            if j == -1:
                raise ValueError(f"unsupported extglob in {seg!r}")
            out.append("(?:" + _class(seg[i + 2:j + 1]) + ")*")
            i = j + 2
        elif c == "*":
            if seg.startswith("**", i):
                raise ValueError(f"`**` inside a segment not modelled ({seg!r})")
            # bash: `.*?` — crosses `/`; default: STAR = `[^/]*?`.
            # Both get NO_DOT when the star opens the segment.
            out.append((_NO_DOT if i == 0 else "") + (".*?" if bash else "[^/]*?"))
            i += 1
        elif c == "[":
            j = seg.find("]", i + 1)
            if j == -1:
                raise ValueError(f"unterminated class in {seg!r}")
            out.append(_class(seg[i:j + 1]))
            i = j + 1
        elif c in _UNSUPPORTED:
            raise ValueError(f"glob metachar {c!r} not modelled (segment {seg!r})")
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _split_segments(pattern: str) -> list[str]:
    """Split on `/` OUTSIDE `[...]` — `*([^/])` carries a slash in its class."""
    segs, cur, depth = [], [], 0
    for ch in pattern:
        if ch == "[":
            depth += 1
        elif ch == "]" and depth:
            depth -= 1
        if ch == "/" and not depth:
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    segs.append("".join(cur))
    return segs


def _glob_to_regex(pattern: str, mode: str) -> re.Pattern[str]:
    if mode not in MODES:
        raise ValueError(mode)
    bash = mode == "bash"
    # yaml-language-server prefixes every fileMatch with `**/`; picomatch then
    # strips consecutive `/**` segments.
    segs: list[str] = []
    for seg in _split_segments("**/" + pattern.lstrip("/")):
        if not seg:
            raise ValueError(f"empty segment in {pattern!r}")
        if not (seg == "**" and segs and segs[-1] == "**"):
            segs.append(seg)
    if segs[-1] == "**":
        raise ValueError("trailing ** not modelled")
    parts: list[str] = []
    for k, seg in enumerate(segs):
        if seg == "**":
            if k == 0:
                # bos globstar + `/`: `(?:^|/|<globstar>/)` — consumes its slash.
                parts.append(rf"(?:^|/|{_GLOBSTAR}/)")
            else:
                # middle `/**/`: the preceding `/` is folded into the group.
                guard = "" if bash else _NO_DOT
                parts.append(rf"(?:/{guard}{_GLOBSTAR}/|/|$)")
            continue
        if k > 0 and segs[k - 1] != "**":
            parts.append("/")
        parts.append(_segment_to_regex(seg, bash))
    return re.compile("".join(parts) + "/?")


def _bound(globs: list[str], uri: str, mode: str) -> bool:
    return any(_glob_to_regex(g, mode).fullmatch(uri) for g in globs)


def _classify(schemas: dict, uri: str, mode: str) -> str:
    t = _bound(schemas[TENANT_SCHEMA_KEY], uri, mode)
    p = _bound(schemas[PLATFORM_SCHEMA_KEY], uri, mode)
    return ("T" if t else "") + ("P" if p else "") or "-"


def editor_classes(schemas: dict, mode: str) -> dict[str, str]:
    return {rel: _classify(schemas, _URI_PREFIX + rel, mode) for rel in CORPUS}


# ---------------------------------------------------------------- CI side
class _RecordingValidator:
    """Stands in for the jsonschema module: records which schema each doc hit."""

    class ValidationError(Exception):
        pass

    def __init__(self) -> None:
        self.seen: dict[str, str] = {}

    def validate(self, doc, schema) -> None:
        self.seen[doc["rel"]] = schema["id"]


@pytest.fixture(scope="module")
def ci(tmp_path_factory) -> dict[str, str]:
    # One tree per corpus entry: the corpus holds case-only pairs
    # (`db-a.yaml` / `db-a.YAML`), which a case-insensitive filesystem
    # (macOS, Windows) collapses into one file when written side by side.
    root = tmp_path_factory.mktemp("parity")
    seen: dict[str, str] = {}
    for i, rel in enumerate(CORPUS):
        confd = root / str(i) / "conf.d"
        p = confd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("rel: " + json.dumps(rel) + "\n", encoding="utf-8")
        rec = _RecordingValidator()
        validate_dir(str(confd), {"id": "T"}, rec, {"id": "P"})
        assert set(rec.seen) <= {rel}, rec.seen
        seen[rel] = rec.seen.get(rel, "-")
    return seen


@pytest.fixture(scope="module")
def schemas() -> dict:
    return _load_yaml_schemas(DEVCONTAINER.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- tests
def test_jsonc_strip_keeps_urls_in_strings():
    text = '{\n  // c\n  "u": "https://x//y", /* b */ "v": "a/*b*/"\n}'
    assert json.loads(_strip_jsonc(text)) == {"u": "https://x//y", "v": "a/*b*/"}


def test_devcontainer_parses_and_declares_both_schemas(schemas):
    assert schemas[TENANT_SCHEMA_KEY] and schemas[PLATFORM_SCHEMA_KEY]
    for key in (TENANT_SCHEMA_KEY, PLATFORM_SCHEMA_KEY):
        assert (REPO_ROOT / key).is_file(), f"yaml.schemas points at missing {key}"


def test_ci_side_must_fire(ci):
    for rel in _MUST_TENANT:
        assert ci[rel] == "T", (rel, ci[rel])
    for rel in _MUST_PLATFORM:
        assert ci[rel] == "P", (rel, ci[rel])


@pytest.mark.parametrize("mode", MODES)
def test_editor_globs_bind_exactly_what_ci_validates(schemas, ci, mode):
    editor = editor_classes(schemas, mode)

    for rel in _MUST_TENANT:
        assert editor[rel] == "T", f"[{mode}] must-fire control not bound: {rel} -> {editor[rel]}"
    for rel in _MUST_PLATFORM:
        assert editor[rel] == "P", f"[{mode}] must-fire control not bound: {rel} -> {editor[rel]}"

    declared = DECLARED_UNBOUND[mode]
    # Every path is compared: outside the declared set the editor must equal
    # CI, inside it the editor must bind NOTHING (and CI must bind something
    # there, or the declaration is vacuous).
    expected = {rel: ("-" if declared(rel) else ci[rel]) for rel in CORPUS}
    drift = {rel: (editor[rel], ci[rel]) for rel in CORPUS if editor[rel] != expected[rel]}
    assert not drift, (
        f"[{mode}] devcontainer.json yaml.schemas vs check_confd_schema.py "
        "(rel: (editor, ci)); T=tenant schema, P=platform-defaults schema:\n"
        + "\n".join(f"  {r}: {v}" for r, v in sorted(drift.items())))
    assert any(declared(rel) and ci[rel] != "-" for rel in CORPUS)


def test_bash_mode_enters_a_first_level_dot_dir(schemas, ci):
    """Pins the half of the dot-dir story that differs between engines."""
    assert editor_classes(schemas, "bash")[".hid/db-a.yaml"] == ci[".hid/db-a.yaml"] == "T"
    assert editor_classes(schemas, "default")[".hid/db-a.yaml"] == "-"


@pytest.mark.parametrize("mode", MODES)
def test_workspace_under_a_dot_dir_binds_nothing(schemas, mode):
    """Disclosed limitation, not a goal: the leading `**` cannot cross a
    dot-directory ABOVE conf.d, so a checkout under `.claude/worktrees/`
    gets no schema in either engine."""
    assert _classify(schemas, _DOT_WORKSPACE_URI, mode) == "-"


def test_translator_refuses_unmodelled_syntax():
    for bad in ("**/conf.d/?.yaml", "**/conf.d/{a,b}.yaml", "**/conf.d/+(a).yaml",
                "**/conf.d/*([^/]).yaml", "**/conf.d/a**b.yaml"):
        for mode in MODES:
            with pytest.raises(ValueError):
                _glob_to_regex(bad, mode)


#: The picomatch vscode-yaml 1.24.0 bundles (its package-lock); ci.yml installs
#: exactly this.
PINNED_PICOMATCH = "4.0.5"


def _picomatch_dir() -> Path | None:
    env = os.environ.get("PICOMATCH_DIR")
    cands = [Path(env)] if env else []
    cands.append(REPO_ROOT / "tests" / "e2e" / "node_modules" / "picomatch")
    return next((c for c in cands if (c / "package.json").is_file()), None)


def test_translator_agrees_with_real_picomatch(schemas):
    """Anchor for the model: the real engine, called the way
    yaml-language-server calls it, in both modes, cell for cell."""
    pm_dir, node = _picomatch_dir(), shutil.which("node")
    if pm_dir is None or node is None:
        msg = ("real picomatch not resolvable (set PICOMATCH_DIR or install "
               "tests/e2e node_modules)")
        if os.environ.get("VIBE_REQUIRE_PICOMATCH") == "1":
            pytest.fail(f"VIBE_REQUIRE_PICOMATCH=1 but {msg} — the CI install "
                        "step regressed")
        pytest.skip(msg + " — parity rests on the translator")
    # The CI install pins the version yaml-language-server 1.24.0 ships; the
    # local fallback (tests/e2e/node_modules) is a transitive dep and may be
    # another version. Under the CI contract, compare against the pinned one.
    version = json.loads((pm_dir / "package.json").read_text(encoding="utf-8"))["version"]
    if os.environ.get("VIBE_REQUIRE_PICOMATCH") == "1":
        assert version == PINNED_PICOMATCH, (
            f"VIBE_REQUIRE_PICOMATCH=1 but {pm_dir} is picomatch {version}, "
            f"not {PINNED_PICOMATCH} — the reference engine drifted")
    script = (
        "const pm=require(process.argv[1]);"
        "const [g,uris]=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "const res={};"
        "for (const [mode,opt] of [['default',{}],['bash',{bash:true}]]){"
        " const m={};for(const [k,v] of Object.entries(g)){"
        "  m[k]=pm(v.map(p=>'**/'+p),Object.assign({noglobstar:false},opt));}"
        " res[mode]=uris.map(u=>(m.T(u)?'T':'')+(m.P(u)?'P':'')||'-');}"
        "console.log(JSON.stringify(res));"
    )
    uris = [_URI_PREFIX + rel for rel in CORPUS] + [_DOT_WORKSPACE_URI]
    payload = json.dumps([{"T": schemas[TENANT_SCHEMA_KEY],
                           "P": schemas[PLATFORM_SCHEMA_KEY]}, uris])
    proc = subprocess.run([node, "-e", script, str(pm_dir)], input=payload,
                          capture_output=True, text=True, check=True, timeout=60)
    real = json.loads(proc.stdout)
    for mode in MODES:
        model = [_classify(schemas, u, mode) for u in uris]
        diff = {u: (r, m) for u, r, m in zip(uris, real[mode], model) if r != m}
        assert not diff, f"[{mode}] translator vs picomatch (real, model): {diff}"
