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
spelling and case, `_defaults*`, other `_` files, hidden names) the set each
schema's globs bind must equal the set CI sends to that schema.

⛔ THE MATCHER IS A MODEL, NOT THE ENGINE. yaml-language-server matches
`yaml.schemas` globs with picomatch (JavaScript); no stdlib Python matcher
agrees with it on these patterns — `pathlib.PurePath.full_match` reads `[^_]`
as the literal class {`^`, `_`} and has no extglob. So `_glob_to_regex`
translates the few picomatch constructs these patterns use (default options,
i.e. `dot: false`), and REFUSES every other glob metacharacter rather than
guessing. Its faithfulness is anchored by
`test_translator_agrees_with_real_picomatch`, which runs the real picomatch
when one is resolvable and is SKIPPED otherwise — in CI's Python Tests job it
is normally skipped, so there the parity check rests on the translator.

Declared, tested difference: picomatch's `**` does not descend into
dot-directories, CI's `os.walk` does. `conf.d/.x/t.yaml` is validated by CI
and bound by no glob; the test asserts exactly that, so a change on either
side surfaces here.
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

# Where the corpus sits for the editor, as an absolute-ish workspace path.
# yaml-language-server matches the whole document path, so the globs must
# find `conf.d` however deep the tree is.
_EDITOR_PREFIX = "workspaces/vibe-k8s-lab/components/threshold-exporter/config/conf.d/"

_NAMES = (
    "db-a.yaml", "db-a.yml", "db-a.YAML", "db-a.Yml", "x.yaml.yml",
    "_defaults.yaml", "_defaults.yml", "_defaults-multidb.yml",
    "_DEFAULTS.YAML", "_Defaults.Yml",
    "_other.yaml", "_routing_profiles.yml", "_.yaml",
    ".hidden.yaml", ".hidden.yml",
    "notes.txt", "db-a.yaml.bak", "yaml",
)
_DIRS = ("", "sub/", "a/b/", "_archive/", ".hid/")
CORPUS = tuple(d + n for d in _DIRS for n in _NAMES)

# Must-fire controls: if the corpus/assembly silently binds nothing, these fail.
_MUST_TENANT = ("db-a.yaml", "db-a.yml", "sub/db-a.YAML", "a/b/db-a.Yml")
_MUST_PLATFORM = ("_defaults.yaml", "_defaults-multidb.yml", "sub/_DEFAULTS.YAML")


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
_UNSUPPORTED = set("?{}()!+@\\")


def _segment_to_regex(seg: str) -> str:
    """One path segment of a picomatch glob (default options) → regex.

    Supported: literals, `*`, `[...]` / `[^...]` / `[!...]`, and the extglob
    `*([...])`. Anything else raises — a model that guesses would turn this
    test into a second, unaudited opinion.
    """
    out: list[str] = []
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if seg.startswith("*([", i):
            j = seg.find("])", i + 3)
            if j == -1:
                raise ValueError(f"unsupported extglob in {seg!r}")
            out.append("(?:" + _class(seg[i + 2:j + 1]) + ")*")
            i = j + 2
        elif c == "*":
            out.append("[^/]*")
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
    body = "".join(out)
    # picomatch `dot: false` guards only a LEADING STAR: `*.yaml` never
    # matches `.x.yaml`, but an explicit class such as `[^_]` does match a
    # leading `.` — measured against picomatch 4.0.7, the reason the tenant
    # globs spell the exclusion `[^_.]`.
    return r"(?!\.)" + body if seg.startswith("*") else body


def _class(cls: str) -> str:
    inner = cls[1:-1]
    if not inner or any(ch in inner for ch in "[\\"):
        raise ValueError(f"character class {cls!r} not modelled")
    if inner[0] in "^!":
        return "[^/" + inner[1:] + "]"
    return "[" + inner + "]"


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


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    # yaml-language-server prefixes every fileMatch with `**/`.
    segs = _split_segments("**/" + pattern.lstrip("/"))
    parts: list[str] = []
    for k, seg in enumerate(segs):
        last = k == len(segs) - 1
        if seg == "**":
            if last:
                raise ValueError("trailing ** not modelled")
            parts.append(r"(?:(?!\.)[^/]+/)*")  # zero+ non-dot segments
        else:
            parts.append(_segment_to_regex(seg) + ("" if last else "/"))
    return re.compile("".join(parts))


def _bound(globs: list[str], path: str) -> bool:
    return any(_glob_to_regex(g).fullmatch(path) for g in globs)


def editor_classes(schemas: dict) -> dict[str, str]:
    tenant, platform = schemas[TENANT_SCHEMA_KEY], schemas[PLATFORM_SCHEMA_KEY]
    out = {}
    for rel in CORPUS:
        path = _EDITOR_PREFIX + rel
        out[rel] = ("T" if _bound(tenant, path) else "") + (
            "P" if _bound(platform, path) else "") or "-"
    return out


# ---------------------------------------------------------------- CI side
class _RecordingValidator:
    """Stands in for the jsonschema module: records which schema each doc hit."""

    class ValidationError(Exception):
        pass

    def __init__(self) -> None:
        self.seen: dict[str, str] = {}

    def validate(self, doc, schema) -> None:
        self.seen[doc["rel"]] = schema["id"]


def ci_classes(tmp_path: Path) -> dict[str, str]:
    confd = tmp_path / "conf.d"
    for rel in CORPUS:
        p = confd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("rel: " + json.dumps(rel) + "\n", encoding="utf-8")
    rec = _RecordingValidator()
    validate_dir(str(confd), {"id": "T"}, rec, {"id": "P"})
    return {rel: rec.seen.get(rel, "-") for rel in CORPUS}


def _in_dot_dir(rel: str) -> bool:
    return any(part.startswith(".") for part in rel.split("/")[:-1])


# ---------------------------------------------------------------- tests
def test_jsonc_strip_keeps_urls_in_strings():
    text = '{\n  // c\n  "u": "https://x//y", /* b */ "v": "a/*b*/"\n}'
    assert json.loads(_strip_jsonc(text)) == {"u": "https://x//y", "v": "a/*b*/"}


def test_devcontainer_parses_and_declares_both_schemas():
    schemas = _load_yaml_schemas(DEVCONTAINER.read_text(encoding="utf-8"))
    assert schemas[TENANT_SCHEMA_KEY] and schemas[PLATFORM_SCHEMA_KEY]
    for key in (TENANT_SCHEMA_KEY, PLATFORM_SCHEMA_KEY):
        assert (REPO_ROOT / key).is_file(), f"yaml.schemas points at missing {key}"


def test_ci_side_must_fire(tmp_path):
    ci = ci_classes(tmp_path)
    for rel in _MUST_TENANT:
        assert ci[rel] == "T", (rel, ci[rel])
    for rel in _MUST_PLATFORM:
        assert ci[rel] == "P", (rel, ci[rel])


def test_editor_globs_bind_exactly_what_ci_validates(tmp_path):
    schemas = _load_yaml_schemas(DEVCONTAINER.read_text(encoding="utf-8"))
    editor = editor_classes(schemas)
    ci = ci_classes(tmp_path)

    for rel in _MUST_TENANT:
        assert editor[rel] == "T", f"must-fire control not bound: {rel} -> {editor[rel]}"
    for rel in _MUST_PLATFORM:
        assert editor[rel] == "P", f"must-fire control not bound: {rel} -> {editor[rel]}"

    drift = {rel: (editor[rel], ci[rel]) for rel in CORPUS
             if not _in_dot_dir(rel) and editor[rel] != ci[rel]}
    assert not drift, (
        "devcontainer.json yaml.schemas and check_confd_schema.py disagree "
        "(rel: (editor, ci)); T=tenant schema, P=platform-defaults schema:\n"
        + "\n".join(f"  {r}: {v}" for r, v in sorted(drift.items())))

    # The one declared difference, pinned in both directions.
    dot = [rel for rel in CORPUS if _in_dot_dir(rel)]
    assert dot and any(ci[rel] != "-" for rel in dot)
    assert all(editor[rel] == "-" for rel in dot), {
        rel: editor[rel] for rel in dot if editor[rel] != "-"}


def test_translator_refuses_unmodelled_syntax():
    for bad in ("**/conf.d/?.yaml", "**/conf.d/{a,b}.yaml", "**/conf.d/+(a).yaml"):
        with pytest.raises(ValueError):
            _glob_to_regex(bad)


def _picomatch_dir() -> Path | None:
    env = os.environ.get("PICOMATCH_DIR")
    cands = [Path(env)] if env else []
    cands.append(REPO_ROOT / "tests" / "e2e" / "node_modules" / "picomatch")
    return next((c for c in cands if (c / "package.json").is_file()), None)


def test_translator_agrees_with_real_picomatch():
    """Anchor for the model: real picomatch, called the way yaml-language-server
    calls it — 1.24.0 passes `{bash: true}`, the next line drops it. Both must
    agree with the translator on this corpus."""
    pm_dir, node = _picomatch_dir(), shutil.which("node")
    if pm_dir is None or node is None:
        pytest.skip("real picomatch not resolvable (set PICOMATCH_DIR or install "
                    "tests/e2e node_modules) — parity rests on the translator")
    schemas = _load_yaml_schemas(DEVCONTAINER.read_text(encoding="utf-8"))
    script = (
        "const pm=require(process.argv[1]);"
        "const [g,paths]=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "const res={};"
        "for (const [mode,opt] of [['default',{}],['bash',{bash:true}]]){"
        " const m={};for(const [k,v] of Object.entries(g)){"
        "  m[k]=pm(v.map(p=>'**/'+p),Object.assign({noglobstar:false},opt));}"
        " res[mode]=paths.map(p=>(m.T(p)?'T':'')+(m.P(p)?'P':'')||'-');}"
        "console.log(JSON.stringify(res));"
    )
    paths = ["/" + _EDITOR_PREFIX + rel for rel in CORPUS]
    payload = json.dumps([{"T": schemas[TENANT_SCHEMA_KEY],
                           "P": schemas[PLATFORM_SCHEMA_KEY]}, paths])
    proc = subprocess.run([node, "-e", script, str(pm_dir)], input=payload,
                          capture_output=True, text=True, check=True, timeout=60)
    real = json.loads(proc.stdout)
    model = editor_classes(schemas)
    assert dict(zip(CORPUS, real["default"])) == model
    # bash mode differs from the model only inside dot-directories.
    bash = dict(zip(CORPUS, real["bash"]))
    assert {r: v for r, v in bash.items() if not _in_dot_dir(r)} == {
        r: v for r, v in model.items() if not _in_dot_dir(r)}
