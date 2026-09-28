"""#1466 — cli-reference examples: one shape, and copy-pasteable.

Both pages declare that every example is the `da-tools <cmd>` shorthand for the
Docker prefix in docs/includes/docker-usage-pattern.md, which mounts only the
current directory at /workspace. Two ways that broke before:

* the EN page spelled most examples as a full `docker run` with its own mounts
  (one copy of the prefix per example, one of them with a `--kubeconfig` flag
  docker does not have) while the ZH page used the shorthand;
* examples passed absolute paths (`/tmp/v1.yaml`, `/data/config/conf.d`) that the
  container cannot see, or wrote output to its own /tmp, gone with `--rm`.

So: the command lines of each ZH code block must equal those of the EN block at
the same position (comments and quoted strings are translated, so they are left
out), and a `da-tools` example may not pass an absolute, `~` or `..` path.
"""

import os
import re

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_ZH = os.path.join(_REPO_ROOT, "docs", "cli-reference.md")
_EN = os.path.join(_REPO_ROOT, "docs", "cli-reference.en.md")

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING = re.compile(r"^#{1,6} ")
_COMMAND = re.compile(r"^\s*(?:\$ )?(da-tools|docker|python3|git|kubectl)\s")
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
# An argument the /workspace mount cannot reach: absolute, home or parent.
_OUTSIDE_MOUNT = re.compile(r"^(?:/|~|\.\./|\$HOME)")


def _sections(text):
    """[[block lines, ...] per heading section], skipping blockquoted fences."""
    sections, blocks, fence, buf = [], [], None, None
    for line in text.split("\n"):
        if fence is None:
            m = _FENCE.match(line)
            if m and not line.lstrip().startswith(">"):
                fence, buf = m.group(1), []
            elif _HEADING.match(line):
                sections.append(blocks)
                blocks = []
            continue
        stripped = line.strip()
        if stripped.startswith(fence) and not stripped.strip(fence[0]):
            blocks.append(buf)
            fence = None
        else:
            buf.append(line)
    sections.append(blocks)
    return sections


def _commands(block):
    """Command lines of one block, continuations joined, comments/strings out."""
    out, i = [], 0
    while i < len(block):
        line = block[i]
        while line.rstrip().endswith("\\") and i + 1 < len(block):
            i += 1
            line = line.rstrip()[:-1] + " " + block[i].strip()
        i += 1
        if not _COMMAND.match(line):
            continue
        line = _QUOTED.sub('"…"', line)
        line = re.sub(r"\s+#\s.*$", "", line)
        out.append(" ".join(line.split()))
    return out


def _mismatches(zh_text, en_text):
    zh, en = _sections(zh_text), _sections(en_text)
    if len(zh) != len(en):
        return [f"section count differs: zh {len(zh)}, en {len(en)}"]
    problems = []
    for n, (zs, es) in enumerate(zip(zh, en)):
        if len(zs) != len(es):
            problems.append(f"section {n}: zh {len(zs)} blocks, en {len(es)}")
            continue
        for b, (zb, eb) in enumerate(zip(zs, es)):
            if _commands(zb) != _commands(eb):
                problems.append(f"section {n} block {b}: "
                                f"zh {_commands(zb)} != en {_commands(eb)}")
    return problems


def _outside_mount(text):
    hits = []
    for blocks in _sections(text):
        for block in blocks:
            for cmd in _commands(block):
                if not cmd.lstrip("$ ").startswith("da-tools "):
                    continue
                for token in re.split(r"[\s=]+", cmd):
                    if _OUTSIDE_MOUNT.match(token) and "://" not in token:
                        hits.append(f"{token}  in  {cmd}")
    return hits


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestDetectors:
    """⛔ Controls: the live pages are clean, so prove both checks can fail."""

    def test_parity_ignores_translated_comments_and_strings(self):
        zh = '# T\n```bash\n# 建立\nda-tools x -m "調整" --a\n```\n'
        en = '# T\n```bash\n# Create\nda-tools x -m "Adjust" --a\n```\n'
        assert _mismatches(zh, en) == []

    @pytest.mark.parametrize("en", [
        # the pre-#1466 EN shape: same command spelled as a full docker run
        "# T\n```bash\ndocker run --rm -v $(pwd):/data img \\\n  x --a\n```\n",
        # an example the EN page lacks
        "# T\n```bash\n```\n",
    ])
    def test_parity_reports_a_different_command(self, en):
        assert _mismatches("# T\n```bash\nda-tools x --a\n```\n", en)

    @pytest.mark.parametrize("cmd,bad", [
        ("da-tools rule-pack-diff --from /tmp/v1.yaml", True),
        ("da-tools baseline -o=/tmp/out", True),
        ("da-tools x --config-dir ../conf.d", True),
        ("da-tools x --config-dir conf.d/ --prometheus http://p:9090", False),
        # docker run with its own mount legitimately names container paths
        ("docker run -v $(pwd)/conf.d:/etc/config img x --config-dir /etc/config",
         False),
    ])
    def test_outside_mount(self, cmd, bad):
        assert bool(_outside_mount(f"```bash\n{cmd}\n```\n")) is bad


class TestCliReference:

    def test_pages_have_examples(self):
        """Anti-vacuity: an unparsed page must not read as parity."""
        n = sum(len(_commands(b)) for s in _sections(_read(_ZH)) for b in s)
        assert n >= 100, f"only {n} command lines parsed from {_ZH}"

    def test_zh_and_en_examples_match(self):
        problems = _mismatches(_read(_ZH), _read(_EN))
        assert not problems, (
            "cli-reference ZH and EN code blocks run different commands (#1466). "
            "ZH is the source; copy its block and translate only the comments:"
            "\n  " + "\n  ".join(problems))

    @pytest.mark.parametrize("path", [_ZH, _EN])
    def test_examples_stay_inside_the_mount(self, path):
        hits = _outside_mount(_read(path))
        assert not hits, (
            f"{os.path.relpath(path, _REPO_ROOT)}: `da-tools` runs in a "
            "container that sees only the current directory (mounted at "
            "/workspace), so these paths do not exist there — or, for output, "
            "vanish with the container. Use a path relative to the repo root "
            "(#1466):\n  " + "\n  ".join(hits))
