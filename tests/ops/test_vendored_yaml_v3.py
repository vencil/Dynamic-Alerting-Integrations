"""The vendored gopkg.in/yaml.v3 is upstream v3.0.1 plus THREE pinned patches.

Both Go modules replace ``gopkg.in/yaml.v3`` with
``components/threshold-exporter/app/third_party/yaml.v3``: upstream v3.0.1
with
- grafana/go-yaml a1d5f0f's ``decode.go`` change (#2681: duplicate mapping
  keys checked through a hash map above 48 keys, so a large mapping no longer
  costs O(n²)). Nothing else from that fork may come along — its block-scalar
  encode, ``Node.DecodeWithOptions``, ``0`` as a ``time.Duration`` and the
  upstream v3.0.2+ backports all change behaviour;
- this repo's own ``decode.go`` change (#2730 §6): a quoted or block scalar
  written with the non-specific tag ``!`` keeps ``Tag == "!"`` on its node,
  so the Go readers can read ``! "true"`` / ``! "<<"`` as PyYAML does;
- this repo's own scanner switch (hub #2486 PR-7c round 6):
  ``Decoder.SpacesOnly``, off by default, makes the scanner take only a space
  as a separator, as PyYAML's does — the domain-policy readers turn it on,
  nothing else does. ``yaml.go``, ``yamlh.go`` and ``scannerc.go``, several
  hunks; with the switch off every branch is upstream's.

How this is checked, without the network and without Go:

1. The allowed changes are the hunks in the ``_PATCHES`` files, each file
   pinned by SHA-256 and by the files and hunk counts it may touch — widening what may differ
   means editing this test, in the same diff a reviewer reads.
2. The vendored file each hunk patches must contain the hunk's new text
   exactly once. Swapping them back for the hunks' old text, last applied
   first, has to give upstream's files.
3. With those swaps, the whole directory's Go module hash (``h1:``, the
   dirhash algorithm ``go.sum`` uses) must equal v3.0.1's, as published by
   sum.golang.org. That covers every other file, byte for byte, including
   files added or removed.

The ``h1:`` value is an independent anchor: ``go mod download -json
gopkg.in/yaml.v3@v3.0.1`` prints it after verifying it against the checksum
database, and it is the line the two ``go.sum`` files carried before the
replace removed it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from _tree import REPO_ROOT as ROOT
from _tree import repo_files

_THIRD_PARTY = ROOT / "components" / "threshold-exporter" / "app" / "third_party"
_VENDORED = _THIRD_PARTY / "yaml.v3"
_PATCH = _THIRD_PARTY / "yaml.v3-uniquekeys.patch"
_NONSPECIFIC_PATCH = _THIRD_PARTY / "yaml.v3-nonspecific-tag.patch"
_SPACES_ONLY_PATCH = _THIRD_PARTY / "yaml.v3-spaces-only.patch"

_MODULE = "gopkg.in/yaml.v3"
_VERSION = "v3.0.1"
# sum.golang.org: `gopkg.in/yaml.v3 v3.0.1 h1:...`
_UPSTREAM_H1 = "h1:fxVm/GzAzEWqLHuvctI91KS9hhNmmWOoWu0XTYJS7CA="
# grafana/go-yaml a1d5f0f's decode.go hunk; its +/- lines are byte-identical
# to that commit, only the @@ line numbers moved by -3 to land on v3.0.1.
_PATCH_SHA256 = "32fd9b238124d4eb520292c97119b2eaed76b983b9d1bc2f16e6d0f429451b9a"
# #2730 §6: (*parser).scalar keeps the tag "!" on a non-plain scalar.
_NONSPECIFIC_PATCH_SHA256 = "e28069e759fe29c3645e48b0aee3af4d49062b6dd4ada440cc0256866a936964"
# Hub #2486 PR-7c round 6: Decoder.SpacesOnly (off by default), the scanner
# taking only a space as a separator, as PyYAML's does.
_SPACES_ONLY_PATCH_SHA256 = "30987acac7b60084bf76703c8620140ceb78d5b1ecb7a817633b0b05557ec8fb"
# Every allowed change, applied in this order on upstream v3.0.1: its pinned
# sha256, and the hunks it may hold per file.
_PATCHES = {
    _PATCH: (_PATCH_SHA256, {"decode.go": 1}),
    _NONSPECIFIC_PATCH: (_NONSPECIFIC_PATCH_SHA256, {"decode.go": 1}),
    _SPACES_ONLY_PATCH: (_SPACES_ONLY_PATCH_SHA256, {"yaml.go": 1, "yamlh.go": 1, "scannerc.go": 15}),
}

# The modules that build with it, and the replace each must carry.
_REPLACES = {
    ROOT / "components" / "threshold-exporter" / "app" / "go.mod": "./third_party/yaml.v3",
    ROOT / "components" / "tenant-api" / "go.mod": "../threshold-exporter/app/third_party/yaml.v3",
}

_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def _hunks(patch: Path) -> list[tuple[str, str, str]]:
    """(file, old text, new text) of each hunk of a unified diff, in order.

    Each hunk is read by its header's line counts, so a removed line that
    happens to start with `-- a/` cannot be taken for a file header."""
    lines = patch.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[tuple[str, str, str]] = []
    i = 0
    while i < len(lines):
        head = lines[i:i + 2]
        assert (len(head) == 2 and head[0].startswith("--- a/") and head[1].startswith("+++ b/")
                and head[0][6:] == head[1][6:]), (
            f"{patch.name}:{i + 1}: expected a `--- a/<file>` / `+++ b/<file>` header, got {head!r}")
        name = head[0][6:].rstrip("\n")
        assert "/" not in name, f"{patch.name}: {name} is not a top-level file of the module"
        i += 2
        assert i < len(lines) and _HUNK.match(lines[i]), f"{patch.name}: {name} has no hunk"
        while i < len(lines) and (m := _HUNK.match(lines[i])):
            n_old, n_new = int(m.group(1) or 1), int(m.group(2) or 1)
            i += 1
            old: list[str] = []
            new: list[str] = []
            while len(old) < n_old or len(new) < n_new:
                assert i < len(lines), f"{patch.name}: a hunk of {name} is cut short"
                tag, text = lines[i][:1], lines[i][1:]
                if tag == " ":
                    old.append(text)
                    new.append(text)
                elif tag == "-":
                    old.append(text)
                elif tag == "+":
                    new.append(text)
                else:
                    raise AssertionError(f"{patch.name}: unexpected line {lines[i]!r}")
                i += 1
            assert (len(old), len(new)) == (n_old, n_new), f"{patch.name}: a hunk of {name} miscounts"
            out.append((name, "".join(old), "".join(new)))
    return out


def _hunk(patch: Path = _PATCH) -> tuple[str, str]:
    """(old text, new text) of a one-hunk patch."""
    (only,) = _hunks(patch)
    return only[1], only[2]


def _dirhash_h1(files: dict[str, bytes]) -> str:
    """golang.org/x/mod/sumdb/dirhash.Hash1 over {relative path: content}."""
    prefix = f"{_MODULE}@{_VERSION}/"
    summary = "".join(
        f"{hashlib.sha256(files[rel]).hexdigest()}  {prefix}{rel}\n"
        for rel in sorted(files, key=lambda r: (prefix + r).encode()))
    return "h1:" + base64.b64encode(hashlib.sha256(summary.encode()).digest()).decode()


def _reverted_h1(tree: dict[str, bytes]) -> str:
    """The tree's h1 after swapping the allowed hunks back to upstream's text
    (last applied first)."""
    texts: dict[str, str] = {}
    for patch in reversed(list(_PATCHES)):
        for name, old, new in reversed(_hunks(patch)):
            assert name in tree, f"{patch.name} patches {name}, which the vendored tree lacks"
            text = texts.setdefault(name, tree[name].decode("utf-8"))
            count = text.count(new)
            assert count == 1, (
                f"vendored {name} carries a patched block of {patch.name} "
                f"{count} time(s), not 1: the hunk was edited, or something else "
                "was changed inside it.")
            texts[name] = text.replace(new, old)
    return _dirhash_h1({**tree, **{name: text.encode() for name, text in texts.items()}})


def _tree(directory: Path) -> dict[str, bytes]:
    return {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in directory.rglob("*") if p.is_file()
    }


def _repo_tree() -> dict[str, bytes]:
    """The vendored directory as the repository holds it (tracked + new files)."""
    out = {
        p.relative_to(_VENDORED).as_posix(): p.read_bytes()
        for p in repo_files() if _VENDORED in p.parents
    }
    assert "decode.go" in out and "go.mod" in out, (
        f"found {sorted(out)[:5]} under {_VENDORED}; the listing lost the "
        "vendored module, and every check below would be vacuous.")
    return out


def test_the_allowed_changes_are_the_pinned_hunks() -> None:
    for patch, (want, files) in _PATCHES.items():
        digest = hashlib.sha256(patch.read_bytes()).hexdigest()
        assert digest == want, (
            f"{patch.name} changed (sha256 {digest}). It defines what may differ "
            "from upstream v3.0.1; widening it is a decision for the owner "
            "(#2681, #2730, #2486), made by editing its pinned sha256 here in the same diff.")
        counts: dict[str, int] = {}
        for name, _, _ in _hunks(patch):
            counts[name] = counts.get(name, 0) + 1
        assert counts == files, f"{patch.name} touches {counts}, pinned {files}"
    old, new = _hunk()
    assert "func (d *decoder) mapping(" in old and "checkUniqueKeysMap" in new
    old, new = _hunk(_NONSPECIFIC_PATCH)
    assert 'n.Tag = "!"' in new and 'n.Tag = "!"' not in old
    # Every hunk of the switch is behind it: a new line that tests no flag is
    # a call of the gated helpers, or the field and the setter.
    for name, old, new in _hunks(_SPACES_ONLY_PATCH):
        added = [line for line in new.splitlines() if line not in old.splitlines()]
        assert any("spaces_only" in line or "is_separator" in line for line in added), (name, added)


def test_vendored_tree_is_v3_0_1_plus_only_the_hunk() -> None:
    got = _reverted_h1(_repo_tree())
    assert got == _UPSTREAM_H1, (
        f"{_VENDORED.relative_to(ROOT)} with the allowed hunk reverted hashes to "
        f"{got}, not v3.0.1's {_UPSTREAM_H1}: some other byte, file or file "
        "name differs from upstream. Re-vendor as third_party/README.md says; "
        "never edit these files by hand.")


def _tokens(line: str) -> list[str]:
    """One go.mod line as Go's lexer splits it: `//` comments dropped, "…"
    strings unquoted, `(` `)` `=>` as their own tokens.

    Only double quotes: Go rejects a backquoted path in go.mod ("invalid quoted
    string", measured with go mod edit), so a backquote fails closed here too.
    """
    out: list[str] = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c.isspace():
            i += 1
        elif line.startswith("//", i):
            break
        elif line.startswith("=>", i):
            out.append("=>")
            i += 2
        elif c in "()":
            out.append(c)
            i += 1
        elif c == '"':
            j = i + 1
            while j < n and line[j] != '"':
                j += 2 if line[j] == "\\" else 1
            if j >= n:
                raise AssertionError(f"unterminated string in go.mod line {line!r}")
            try:
                out.append(json.loads(line[i:j + 1]))
            except ValueError as e:
                raise AssertionError(f"unreadable string in go.mod line {line!r}: {e}") from e
            i = j + 1
        elif c == "`":
            raise AssertionError(f"backquoted string in go.mod line {line!r} (Go rejects it)")
        else:
            j = i
            while (j < n and not line[j].isspace() and line[j] not in '()"`'
                   and not line.startswith("//", j) and not line.startswith("=>", j)):
                j += 1
            out.append(line[i:j])
            i = j
    return out


def _parse_replaces(text: str) -> list[dict]:
    """Every `replace` directive in a go.mod, in `go mod edit -json` shape.

    Pure Python on purpose: the guard must run where no Go toolchain is
    installed (CI's Python Tests job has no setup-go), or it is a check that
    never runs. Handles the one-line and `replace ( … )` block forms, optional
    versions on either side, trailing `//` comments and quoted paths.
    """
    out: list[dict] = []
    in_block = False
    for lineno, line in enumerate(text.splitlines(), 1):
        toks = _tokens(line)
        if not toks:
            continue
        if in_block:
            if toks == [")"]:
                in_block = False
                continue
            spec = toks
        elif toks[0] == "replace":
            if toks[1:] == ["("]:
                in_block = True
                continue
            spec = toks[1:]
        else:
            continue
        if "=>" not in spec:
            raise AssertionError(f"go.mod:{lineno}: replace without `=>`: {line!r}")
        k = spec.index("=>")
        old, new = spec[:k], spec[k + 1:]
        if len(old) not in (1, 2) or len(new) not in (1, 2):
            raise AssertionError(f"go.mod:{lineno}: malformed replace: {line!r}")
        entry: dict = {"Old": {"Path": old[0]}, "New": {"Path": new[0]}}
        if len(old) == 2:
            entry["Old"]["Version"] = old[1]
        if len(new) == 2:
            entry["New"]["Version"] = new[1]
        out.append(entry)
    if in_block:
        raise AssertionError("go.mod: unterminated `replace (` block")
    return out


def _replace_problems(text: str, target: str, base: Path) -> list[str]:
    found = [r for r in _parse_replaces(text) if r["Old"]["Path"] == _MODULE]
    if len(found) != 1:
        return [f"{len(found)} replace(s) of {_MODULE}, want exactly 1: {found}"]
    (r,) = found
    problems = []
    if r["Old"].get("Version"):
        problems.append(f"the replace pins Old.Version {r['Old']['Version']!r}; it must "
                        "cover every version")
    if r["New"]["Path"] != target or r["New"].get("Version"):
        problems.append(f"New is {r['New']}, want the directory {target!r}")
    elif (not (base / target / "go.mod").is_file()
          or (base / target).resolve() != _VENDORED.resolve()):
        problems.append(f"{target!r} is not the vendored module {_VENDORED}")
    return problems


_EXPORTER_GOMOD = ROOT / "components" / "threshold-exporter" / "app" / "go.mod"
_LINE = "replace gopkg.in/yaml.v3 => ./third_party/yaml.v3"


def _exporter_with(old: str, new: str) -> str:
    text = _EXPORTER_GOMOD.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"the exporter go.mod no longer has {old!r}"
    return text.replace(old, new)


def _exporter_problems(text: str) -> list[str]:
    return _replace_problems(text, _REPLACES[_EXPORTER_GOMOD], _EXPORTER_GOMOD.parent)


def test_both_modules_build_with_the_vendored_copy() -> None:
    for gomod, target in _REPLACES.items():
        problems = _replace_problems(gomod.read_text(encoding="utf-8"), target, gomod.parent)
        assert not problems, f"{gomod.relative_to(ROOT)}: {problems}"


@pytest.mark.parametrize("extra", [
    "replace gopkg.in/yaml.v3 v3.0.1 => ../elsewhere\n",
    "replace (\n\tgopkg.in/yaml.v3 v3.0.1 => ../elsewhere\n)\n",
])
def test_an_extra_versioned_replace_is_caught(extra: str) -> None:
    """Go prefers a versioned replace over the unversioned one, so this line
    would silently swap the vendored copy out; the check must count it."""
    text = _EXPORTER_GOMOD.read_text(encoding="utf-8") + extra
    assert _exporter_problems(text) != []


def test_a_block_form_replace_is_read() -> None:
    text = _exporter_with(_LINE, "replace (\n\tgopkg.in/yaml.v3 => ./third_party/yaml.v3\n)")
    assert _exporter_problems(text) == []


def test_a_trailing_comment_is_ignored() -> None:
    assert _exporter_problems(_exporter_with(_LINE, _LINE + " // => ../elsewhere")) == []
    # A commented-out directive is no directive: zero replaces, so red.
    assert _exporter_problems(_exporter_with(_LINE, "// " + _LINE)) != []


def test_quoted_paths_are_unquoted() -> None:
    quoted = 'replace "gopkg.in/yaml.v3" => "./third_party/yaml.v3"'
    assert _exporter_problems(_exporter_with(_LINE, quoted)) == []
    extra = '\nreplace "gopkg.in/yaml.v3" v3.0.1 => "../elsewhere"\n'
    assert _exporter_problems(_EXPORTER_GOMOD.read_text(encoding="utf-8") + extra) != []
    with pytest.raises(AssertionError, match="backquoted"):
        _parse_replaces('replace gopkg.in/yaml.v3 => `./third_party/yaml.v3`\n')


@pytest.mark.parametrize("gomod", sorted(_REPLACES), ids=lambda p: p.parent.name)
def test_python_parser_agrees_with_go(gomod: Path, tmp_path: Path) -> None:
    """Cross-check only: the guard above never needs Go, this one does."""
    go = shutil.which("go")
    if go is None:
        pytest.skip("no `go` on PATH; the pure-Python replace guard still ran")
    samples = {
        "real": gomod.read_text(encoding="utf-8"),
        "tricky": gomod.read_text(encoding="utf-8") + (
            'replace(\n\t"gopkg.in/yaml.v3" v3.0.1 => "../elsewhere" // c\n'
            "\texample.com/a => example.com/b v1.2.3\n)\n"
            'replace "example.com/c" => ./d // "quoted" => in a comment\n'),
    }
    for name, text in samples.items():
        f = tmp_path / f"{name}.mod"
        f.write_text(text, encoding="utf-8")
        proc = subprocess.run(
            [go, "mod", "edit", "-json", str(f)], capture_output=True, text=True,
            encoding="utf-8", timeout=60,
            env={**os.environ, "GOTOOLCHAIN": "local", "GOFLAGS": ""})
        assert proc.returncode == 0, f"go mod edit -json {name}: {proc.stderr}"
        assert _parse_replaces(text) == (json.loads(proc.stdout).get("Replace") or []), name


# ── the guard catches what it exists to catch ──────────────────────────────


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    dst = tmp_path / "yaml.v3"
    shutil.copytree(_VENDORED, dst)
    return dst


def test_pristine_copy_passes(scratch: Path) -> None:
    assert _reverted_h1(_tree(scratch)) == _UPSTREAM_H1


def test_a_byte_outside_the_hunk_is_caught(scratch: Path) -> None:
    f = scratch / "scannerc.go"
    # The package clause: upstream's, outside every hunk.
    f.write_bytes(f.read_bytes().replace(b"\npackage yaml\n", b"\npackage yamL\n", 1))
    assert _reverted_h1(_tree(scratch)) != _UPSTREAM_H1


def test_a_byte_inside_the_hunk_is_caught(scratch: Path) -> None:
    f = scratch / "decode.go"
    f.write_bytes(f.read_bytes().replace(
        b"const uniqueKeysScanLimit = 48", b"const uniqueKeysScanLimit = 49"))
    with pytest.raises(AssertionError, match="uniquekeys.patch 0 time"):
        _reverted_h1(_tree(scratch))


def test_a_byte_inside_the_spaces_only_hunk_is_caught(scratch: Path) -> None:
    f = scratch / "scannerc.go"
    f.write_bytes(f.read_bytes().replace(
        b"if parser.spaces_only && *indent == 0 {", b"if *indent == 0 {"))
    with pytest.raises(AssertionError, match="spaces-only.patch 0 time"):
        _reverted_h1(_tree(scratch))


def test_a_byte_inside_the_nonspecific_hunk_is_caught(scratch: Path) -> None:
    f = scratch / "decode.go"
    f.write_bytes(f.read_bytes().replace(
        b'if nodeTag == "!" && nodeStyle != 0 {', b'if nodeTag == "!" {'))
    with pytest.raises(AssertionError, match="nonspecific-tag.patch 0 time"):
        _reverted_h1(_tree(scratch))


def test_an_added_file_is_caught(scratch: Path) -> None:
    (scratch / "duration.go").write_text("package yaml\n", encoding="utf-8")
    assert _reverted_h1(_tree(scratch)) != _UPSTREAM_H1


def test_a_removed_file_is_caught(scratch: Path) -> None:
    (scratch / "suite_test.go").unlink()
    assert _reverted_h1(_tree(scratch)) != _UPSTREAM_H1
