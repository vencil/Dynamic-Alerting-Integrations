"""#1447 — a command a tool prints for a human must name something they have.

The tools reachable through ``da-tools`` ship as a container image.  Their
audience is routinely a customer whose repository holds ``conf.d/`` and
whatever ``da-tools init`` wrote: no ``scripts/``, no ``docs/``, no checkout
of this repository at all.  Five printed lines told that reader to run
``python3 scripts/tools/patch_config.py`` (and ``diagnose.py`` /
``check_alert.py``) — paths that had not existed **here** either since the
tools moved under ``scripts/tools/ops/``.

⛔ Scope is deliberately narrow.  A blanket "every repo path mentioned in a
string must exist" check was prototyped twice in this repo and rejected both
times for noise — see the scope notes in
``scripts/tools/lint/check_doc_k8s_refs.py`` ("Broad repo-path existence is
deliberately NOT linted") and ``scripts/tools/lint/check_doc_datools_cmds.py``
("~88 false positives ... the same noise that sank a broad path-existence
lint").  What is checked here is not "a path appears" but "**an invocation**
appears" — a string that reads as ``python3 <path>`` / ``bash <path>``, i.e.
something the reader is being told to run.  Measured on the tree that fixed
#1447: five hits before, zero after, and no false positives in between.

⚠️ What this deliberately does NOT cover, stated so the zero above is not
read as "nothing is stale anywhere": docstrings are skipped, and **seven**
module docstrings still carry a ``Usage:`` line invoking the pre-``ops/``
path (``python3 scripts/tools/<tool>.py``) — ``baseline_discovery``,
``blind_spot_discovery``, ``config_diff``, ``generate_alertmanager_routes``,
``lint_custom_rules``, ``scaffold_tenant``, ``validate_config``.  Those are
read by whoever opens the source, not printed at a user, so they are a
different (maintainer-facing) problem, left for a follow-up rather than
folded in here.  ⛔ No issue number is cited because none has been filed
yet — writing one here before it exists is how a reader ends up trusting a
pointer to nothing.
"""

import ast
import os
import re

import pytest

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".."))
_ENTRYPOINT = os.path.join(
    _REPO_ROOT, "components", "da-tools", "app", "entrypoint.py")

# `python3 scripts/x.py`, `bash tests/y.sh`, … — an interpreter followed by a
# repo-relative path is an instruction to run something.
_INVOCATION = re.compile(
    r"\b(?:python3?|bash|sh)\s+((?:scripts|tests|docs)/[\w./-]+\.(?:py|sh))"
)


def _command_map():
    """The dispatch table, parsed from the image entry point.

    Deriving the population from the entry point rather than listing tools
    here means a newly dispatched tool is covered the day it is added.
    """
    tree = ast.parse(open(_ENTRYPOINT, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "COMMAND_MAP":
                    return ast.literal_eval(node.value)
    raise AssertionError("COMMAND_MAP not found in entrypoint.py")


def _dispatch_sources():
    wanted = set(_command_map().values())
    found = {}
    tools_root = os.path.join(_REPO_ROOT, "scripts", "tools")
    for dirpath, _dirnames, filenames in os.walk(tools_root):
        for name in filenames:
            if name in wanted:
                found[name] = os.path.join(dirpath, name)
    return found


def _docstring_lines(tree):
    """Line numbers covered by any docstring, module / class / function.

    A docstring describes the code to whoever opens the source; it is not
    printed at a user, and it legitimately carries historical paths. The
    first version of this skipped only the *module* docstring — blind review
    pointed out that the stated reason covers function docstrings identically,
    so moving a `Usage:` block into `main.__doc__` (a tidy-up, nothing more)
    turned the guard red and the message told the author to rewrite it as a
    `da-tools` command.
    """
    covered = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            covered.update(range(
                first.lineno, (first.end_lineno or first.lineno) + 1))
    return covered


# Stands in for an f-string's `{...}`. Any character that cannot appear in a
# path works; this one is deliberately not path-legal so a substituted value
# breaks the match instead of silently completing a filename.
_PLACEHOLDER = "\x00"


def _printed_strings(tree):
    """String literals a user could read, minus docstrings.

    f-strings keep their shape: each `{...}` becomes a placeholder rather
    than vanishing. Concatenating only the literal halves of
    `f"python3 scripts/tools/ops/{tool}.py"` produces
    `scripts/tools/ops/.py`, a path that exists nowhere and never will —
    the guard then reports a file the author did not write.
    """
    skip = _docstring_lines(tree)
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) in skip:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                part.value if isinstance(part, ast.Constant)
                and isinstance(part.value, str) else _PLACEHOLDER
                for part in node.values
            )
            if text.strip(_PLACEHOLDER):
                yield node.lineno, text


def _dangling_invocations(source_path):
    tree = ast.parse(open(source_path, encoding="utf-8").read())
    out = []
    for lineno, text in _printed_strings(tree):
        for match in _INVOCATION.finditer(text):
            target = match.group(1)
            if not os.path.exists(os.path.join(_REPO_ROOT, target)):
                out.append((lineno, target))
    return sorted(set(out))


class TestDetectorHasTeeth:
    """⛔ Controls first.

    Every real hit is fixed, so the sweep below now finds nothing — and a
    detector that finds nothing is indistinguishable from a detector that
    cannot find anything.  These two cases are what separate them.
    """

    @pytest.mark.parametrize("snippet", [
        'print("run: python3 scripts/tools/patch_config.py db-a k v")',
        'lines = ["python3 scripts/tools/no_such_zz9.py"]',
        'msg = f"bash scripts/ops/no_such_zz9.sh {name}"',
    ])
    def test_a_dangling_invocation_is_reported(self, tmp_path, snippet):
        probe = tmp_path / "probe.py"
        probe.write_text(snippet + "\n", encoding="utf-8", newline="\n")
        assert _dangling_invocations(str(probe)), (
            f"detector missed a dangling invocation in: {snippet}")

    @pytest.mark.parametrize("snippet", [
        # Exists on disk — must be tolerated.
        'print("python3 scripts/tools/ops/patch_config.py db-a k v")',
        # The subcommand form: no path at all, which is the whole point.
        'print("da-tools patch-config db-a k v")',
        # A path mentioned but not invoked — that is the noisy class this
        # check deliberately stays out of.
        'HINT = "see scripts/tools/no_such_zz9.py for the shape"',
        # f-string with the tool name interpolated: the literal halves join
        # into `scripts/tools/ops/.py`, a file that exists nowhere and that
        # nobody wrote. Reporting it sends the author to edit a string that
        # is correct.
        'print(f"  python3 scripts/tools/ops/{tool}.py {tenant}")',
        'print(f"bash scripts/ops/{name}.sh")',
        # A docstring is read by whoever opens the source, not printed at a
        # user — and that is true wherever the docstring sits.
        'def main():\n    """Usage: python3 scripts/tools/legacy_zz9.py"""\n',
        'class T:\n    """Usage: python3 scripts/tools/legacy_zz9.py"""\n',
    ])
    def test_legitimate_strings_are_left_alone(self, tmp_path, snippet):
        probe = tmp_path / "probe.py"
        probe.write_text(snippet + "\n", encoding="utf-8", newline="\n")
        assert not _dangling_invocations(str(probe)), (
            f"detector fired on a legitimate string: {snippet}")


class TestNoDispatchedToolPrintsADeadPath:

    def test_population_is_not_empty(self):
        """Anti-vacuity: an empty sweep must not read as compliance."""
        sources = _dispatch_sources()
        assert len(sources) >= 40, (
            f"only resolved {len(sources)} dispatched tools — the walk or the "
            "COMMAND_MAP shape changed, so the sweep below is not looking at "
            "the population it claims to")

    def test_no_dangling_invocations(self):
        problems = []
        for name, path in sorted(_dispatch_sources().items()):
            for lineno, target in _dangling_invocations(path):
                rel = os.path.relpath(path, _REPO_ROOT).replace(os.sep, "/")
                problems.append(f"{rel}:{lineno} -> {target}")
        assert not problems, (
            "these tools print a command naming a path that does not exist "
            "in this repository (#1447):\n  " + "\n  ".join(problems)
            + "\n\nTwo readings, and they need different fixes:"
              "\n  * the path is meant to be OURS — it moved (the tools went "
              "under scripts/tools/ops/) and the reader is usually a customer "
              "with no checkout at all, so print the `da-tools <subcommand>` "
              "form rather than a corrected path;"
              "\n  * the path is meant to be the READER'S, in a repository "
              "this guard cannot see — then the string is fine and the check "
              "is wrong about it. Say so here rather than mangling the "
              "output to get green.")


# `da-tools <subcommand>` in a *command position*: inside backticks, or at
# the start of the string (leading whitespace and a `$ ` prompt allowed).
#
# ⛔ Prose has to be excluded, and not by listing English words. The first
# version matched `da-tools` followed by any lowercase token and flagged
# "Used by da-tools for upgrade detection." — a comment, where `for` is a
# preposition. Requiring a command position is a property of the text rather
# than a list of words that would need a second entry the next time someone
# writes "da-tools also …".
_SUBCOMMAND = re.compile(
    r"(?:`|^|\n)\s*(?:\$\s+)?da-tools\s+([a-z][a-z0-9-]*)\b")


class TestPrintedSubcommandsExist:
    """The replacement for a dead path must not be a dead subcommand.

    Swapping ``python3 scripts/tools/patch_config.py`` for
    ``da-tools patch-config`` moves the failure mode rather than removing it
    if the subcommand is misspelled: the reader gets an argparse error
    instead of "no such file", and nothing in the sweep above notices,
    because there is no path to be missing. COMMAND_MAP is the authority and
    it is already parsed here.
    """

    @staticmethod
    def _printed_subcommands(source_path):
        tree = ast.parse(open(source_path, encoding="utf-8").read())
        found = set()
        for _lineno, text in _printed_strings(tree):
            found.update(_SUBCOMMAND.findall(text))
        return found

    @pytest.mark.parametrize("snippet,expected", [
        ('print("da-tools diagnose t1")', {"diagnose"}),
        ('print("  da-tools patch-config t1 k v")', {"patch-config"}),
        ('print("run `da-tools alert-quality --config-dir conf.d/`")',
         {"alert-quality"}),
        # Prose: `for` is a preposition, not a subcommand.
        ('# Used by da-tools for upgrade detection.', set()),
        # The bare product name with no command after it.
        ('print("（`da-tools` 的取得方式見 https://example.invalid/）")', set()),
    ])
    def test_detector_reads_command_position_only(self, tmp_path, snippet,
                                                  expected):
        probe = tmp_path / "probe.py"
        probe.write_text(snippet + "\n", encoding="utf-8", newline="\n")
        assert self._printed_subcommands(str(probe)) == expected

    def test_detector_can_still_say_no(self, tmp_path):
        """⛔ Control: real data is clean, so prove it can still reject."""
        known = set(_command_map())
        bad = tmp_path / "bad.py"
        bad.write_text('print("da-tools diagnoze t1")\n',
                       encoding="utf-8", newline="\n")
        assert not self._printed_subcommands(str(bad)) <= known

    @pytest.mark.parametrize("snippet,flagged", [
        ('print("da-tools also reads conf.d/")', "also"),
        ('print("da-tools requires kubectl for this subcommand")', "requires"),
        ('print("da-tools not found on PATH; see the install guide")', "not"),
        ('print("da-tools is a container entry point")', "is"),
        ('print("da-tools v2.10.0")', "v2"),
    ])
    def test_english_prose_after_the_product_name_is_a_known_false_positive(
            self, tmp_path, snippet, flagged):
        """⛔ Recorded, not fixed — the information to separate it is absent.

        A printed line beginning ``da-tools also reads conf.d/`` is prose and
        ``da-tools diagnose prod-pg`` is a command; both are the product name
        followed by a bare word and positional-looking tokens, with no flag,
        no backtick and no punctuation between them. Narrowing to backticks
        only would drop the scaffold and baseline lines this change
        introduced, which is the coverage worth having.

        ⚠️ The class is wider than one word — blind review enumerated five,
        including a version banner — and the reason today's tree is green is
        that every `da-tools` sentence in these tools is written in Chinese,
        which the ``[a-z]`` token cannot match. The first English one will
        trip this. Parametrised so that stays a measured statement rather
        than a footnote.

        The cheapest way to clear such a red is to rewrite the sentence ("The
        `da-tools` CLI also reads …") — an improvement. For the version
        banner it is not, and that case is the honest limit of this check.
        """
        probe = tmp_path / "probe.py"
        probe.write_text(snippet + "\n", encoding="utf-8", newline="\n")
        assert self._printed_subcommands(str(probe)) == {flagged}

    def test_chinese_prose_is_why_the_tree_is_currently_green(self, tmp_path):
        """The load-bearing accident, pinned so it is not mistaken for design."""
        probe = tmp_path / "probe.py"
        probe.write_text('print("da-tools 映像本身不含 kubectl")\n',
                         encoding="utf-8", newline="\n")
        assert self._printed_subcommands(str(probe)) == set()

    def test_no_tool_prints_an_unknown_subcommand(self):
        known = set(_command_map())
        assert known, "COMMAND_MAP came back empty"
        problems = []
        for name, path in sorted(_dispatch_sources().items()):
            for sub in sorted(self._printed_subcommands(path) - known):
                rel = os.path.relpath(path, _REPO_ROOT).replace(os.sep, "/")
                problems.append(f"{rel}: da-tools {sub}")
        assert not problems, (
            "these tools tell the reader to run a `da-tools` subcommand that "
            "the image does not dispatch:\n  " + "\n  ".join(problems))
