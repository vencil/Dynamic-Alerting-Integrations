"""The vendored gopkg.in/yaml.v3 is upstream v3.0.1 plus ONE hunk (#2681).

Both Go modules replace ``gopkg.in/yaml.v3`` with
``components/threshold-exporter/app/third_party/yaml.v3``: upstream v3.0.1
with grafana/go-yaml a1d5f0f's ``decode.go`` change (duplicate mapping keys
checked through a hash map above 48 keys, so a large mapping no longer costs
O(n²)). Nothing else from that fork may come along — its block-scalar encode,
``Node.DecodeWithOptions``, ``0`` as a ``time.Duration`` and the upstream
v3.0.2+ backports all change behaviour.

How this is checked, without the network and without Go:

1. The allowed change is the hunk in ``third_party/yaml.v3-uniquekeys.patch``,
   and that file is pinned by SHA-256 here — widening what may differ means
   editing this test, in the same diff a reviewer reads.
2. The vendored ``decode.go`` must contain the hunk's new text exactly once.
   Swapping it back for the hunk's old text has to give upstream's file.
3. With that one swap, the whole directory's Go module hash (``h1:``, the
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

_MODULE = "gopkg.in/yaml.v3"
_VERSION = "v3.0.1"
# sum.golang.org: `gopkg.in/yaml.v3 v3.0.1 h1:...`
_UPSTREAM_H1 = "h1:fxVm/GzAzEWqLHuvctI91KS9hhNmmWOoWu0XTYJS7CA="
# grafana/go-yaml a1d5f0f's decode.go hunk; its +/- lines are byte-identical
# to that commit, only the @@ line numbers moved by -3 to land on v3.0.1.
_PATCH_SHA256 = "32fd9b238124d4eb520292c97119b2eaed76b983b9d1bc2f16e6d0f429451b9a"

# The modules that build with it, and the replace each must carry.
_REPLACES = {
    ROOT / "components" / "threshold-exporter" / "app" / "go.mod": "./third_party/yaml.v3",
    ROOT / "components" / "tenant-api" / "go.mod": "../threshold-exporter/app/third_party/yaml.v3",
}

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


def _hunk() -> tuple[str, str]:
    """(old text, new text) of the patch's only hunk, against decode.go."""
    lines = _PATCH.read_text(encoding="utf-8").splitlines(keepends=True)
    assert lines[:2] == ["--- a/decode.go\n", "+++ b/decode.go\n"], (
        f"{_PATCH.name} must patch decode.go and nothing else; it starts "
        f"{lines[:2]!r}.")
    headers = [i for i, line in enumerate(lines) if _HUNK.match(line)]
    assert headers == [2], f"{_PATCH.name} must hold exactly one hunk, right after the header."
    old: list[str] = []
    new: list[str] = []
    for line in lines[3:]:
        tag, text = line[:1], line[1:]
        if tag == " ":
            old.append(text)
            new.append(text)
        elif tag == "-":
            old.append(text)
        elif tag == "+":
            new.append(text)
        else:
            raise AssertionError(f"{_PATCH.name}: unexpected line {line!r}")
    return "".join(old), "".join(new)


def _dirhash_h1(files: dict[str, bytes]) -> str:
    """golang.org/x/mod/sumdb/dirhash.Hash1 over {relative path: content}."""
    prefix = f"{_MODULE}@{_VERSION}/"
    summary = "".join(
        f"{hashlib.sha256(files[rel]).hexdigest()}  {prefix}{rel}\n"
        for rel in sorted(files, key=lambda r: (prefix + r).encode()))
    return "h1:" + base64.b64encode(hashlib.sha256(summary.encode()).digest()).decode()


def _reverted_h1(tree: dict[str, bytes]) -> str:
    """The tree's h1 after swapping the allowed hunk back to upstream's text."""
    old, new = _hunk()
    decode = tree["decode.go"].decode("utf-8")
    count = decode.count(new)
    assert count == 1, (
        f"vendored decode.go carries the patched block {count} time(s), not 1: "
        "the duplicate-key hunk was edited, or something else was changed "
        "inside it.")
    return _dirhash_h1({**tree, "decode.go": decode.replace(new, old).encode()})


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


def test_the_allowed_change_is_the_pinned_hunk() -> None:
    digest = hashlib.sha256(_PATCH.read_bytes()).hexdigest()
    assert digest == _PATCH_SHA256, (
        f"{_PATCH.name} changed (sha256 {digest}). It defines what may differ "
        "from upstream v3.0.1; widening it is a decision for #2681's owner, "
        "made by editing _PATCH_SHA256 here in the same diff.")
    old, new = _hunk()
    assert "func (d *decoder) mapping(" in old and "checkUniqueKeysMap" in new


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
    f.write_bytes(f.read_bytes().replace(b"yaml_", b"yamL_", 1))
    assert _reverted_h1(_tree(scratch)) != _UPSTREAM_H1


def test_a_byte_inside_the_hunk_is_caught(scratch: Path) -> None:
    f = scratch / "decode.go"
    f.write_bytes(f.read_bytes().replace(
        b"const uniqueKeysScanLimit = 48", b"const uniqueKeysScanLimit = 49"))
    with pytest.raises(AssertionError, match="patched block 0 time"):
        _reverted_h1(_tree(scratch))


def test_an_added_file_is_caught(scratch: Path) -> None:
    (scratch / "duration.go").write_text("package yaml\n", encoding="utf-8")
    assert _reverted_h1(_tree(scratch)) != _UPSTREAM_H1


def test_a_removed_file_is_caught(scratch: Path) -> None:
    (scratch / "suite_test.go").unlink()
    assert _reverted_h1(_tree(scratch)) != _UPSTREAM_H1
