#!/usr/bin/env python3
"""Tests for check_cli_contract (#1379 / TRK-370): every verdict, carrier and
ledger rule has a planted positive and a must-not-report negative on synthetic
docs and parsers; TestRealRepo pins the shipped tree and ledger with probes that
must be found and negatives that must not be.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_cli_contract.py"


def _load():
    sys.path.insert(0, str(SCRIPT.parent))
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
    spec = importlib.util.spec_from_file_location("check_cli_contract", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


mod = _load()


_va_spec = importlib.util.spec_from_file_location(
    "validate_all", REPO_ROOT / "scripts" / "tools" / "validate_all.py")
validate_all = importlib.util.module_from_spec(_va_spec)
assert _va_spec.loader is not None
_va_spec.loader.exec_module(validate_all)


# ---------------------------------------------------------------------------
# Synthetic contract: real argparse parsers through the production `_model`
# ---------------------------------------------------------------------------
def _widget_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="widget")
    p.add_argument("tenant")
    p.add_argument("-o", "--output-dir")
    p.add_argument("--json-output", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--config-dir")
    p.add_argument("--config-file")
    p.add_argument("--tenant-config")
    p.add_argument("--rounds", type=int)
    p.add_argument("--format")
    p.add_argument("--tags", nargs="*")
    return p


def _tool2_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tool2")
    p.add_argument("-r", "--repo")
    sub = p.add_subparsers(dest="action")
    snap = sub.add_parser("snapshot")
    snap.add_argument("-m", "--message")
    log = sub.add_parser("log")
    log.add_argument("--limit", type=int)
    return p


def _tool3_parser() -> argparse.ArgumentParser:
    """Two positional TOKENS before the action slot (nargs=2)."""
    p = argparse.ArgumentParser(prog="tool3")
    p.add_argument("pair", nargs=2)
    sub = p.add_subparsers(dest="action")
    sub.add_parser("snapshot").add_argument("--message")
    return p


COMMAND_MAP = {"widget": "widget.py", "tool2": "tool2.py", "tool3": "tool3.py",
               "noparser": "noparser.py"}
PARSERS = {"widget": mod._model(_widget_parser()), "tool2": mod._model(_tool2_parser()),
           "tool3": mod._model(_tool3_parser())}
INJECTED = {"widget"}

_WIDGET_SOURCE = """\
import sys
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, die_caller_error
EXIT_NETWORK_BLOCKED = 3

def main():
    if bad:
        die_caller_error("x")
    if blocked:
        sys.exit(EXIT_NETWORK_BLOCKED)
    return EXIT_VIOLATION if findings else EXIT_OK

if __name__ == "__main__":
    sys.exit(main())
"""
_OPAQUE_SOURCE = "import sys\nrc = compute()\nsys.exit(rc)\n"

_REF_HEAD = "#### widget\n\n**選項**\n\n| 選項 | 說明 | 預設值 |\n|------|------|--------|\n"
_REF_CLEAN_ROW = "| `--config-dir <PATH>` | dir | `./conf.d` |\n"
_REF_EXIT = ("\n**結束碼**\n\n| 代碼 | 說明 |\n|------|------|\n"
             "| `0` | ok |\n| `1` | findings |\n| `2` | caller |\n| `3` | blocked |\n")


def _reference(tmp_path: Path, rows: str = _REF_CLEAN_ROW,
               exit_table: str = _REF_EXIT, name: str = "ref.md") -> Path:
    p = tmp_path / name
    p.write_text(_REF_HEAD + rows + exit_table, encoding="utf-8")
    return p


def _doc(tmp_path: Path, body: str, name: str = "doc.md") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def _fence(*lines: str, lang: str = "bash") -> str:
    return f"```{lang}\n" + "\n".join(lines) + "\n```\n"


def _scan(tmp_path: Path, docs: list[Path] | None = None,
          reference: Path | None = None, exit_codes=None, parsers=None,
          command_map=None):
    if exit_codes is None:
        exit_codes = {"widget": mod.reachable_exit_codes(_WIDGET_SOURCE)}
    return mod.scan(parsers=PARSERS if parsers is None else parsers,
                    command_map=COMMAND_MAP if command_map is None else command_map,
                    docs=docs if docs is not None else [],
                    reference_docs=(reference or _reference(tmp_path),),
                    injected=INJECTED, exit_codes=exit_codes, repo_root=tmp_path)


def _open(findings):
    return [(f.verdict, f.command, f.token) for f in findings if f.ignored is None]


def _fence_scan(tmp_path: Path, *lines: str):
    return _scan(tmp_path, docs=[_doc(tmp_path, _fence(*lines))])


# ---------------------------------------------------------------------------
class TestV0UnknownSubcommand:
    def test_an_unknown_subcommand_is_reported(self, tmp_path):
        r = _fence_scan(tmp_path, "da-tools describe-tenant --json")
        assert _open(r.findings) == [("V0", "describe-tenant", "describe-tenant")]

    @pytest.mark.parametrize("line", [
        "da-tools widget db-a --json-output",
        "da-tools --help",
        "da-tools help",
        "da-tools --version",
        "da-tools <command> --help",       # placeholder, disclosed not judged
        "da-tools",
    ])
    def test_real_and_placeholder_subcommands_are_not_reported(self, tmp_path, line):
        r = _fence_scan(tmp_path, line)
        assert _open(r.findings) == [], line
        if "<command>" in line:
            assert r.stats["cmd_placeholder_subcommands"] == 1

    def test_a_flag_in_the_subcommand_slot_is_unknown_to_the_dispatcher(self, tmp_path):
        """entrypoint takes argv[1] as the command, so `da-tools --prometheus X
        widget` dies with rc=2 before any script runs."""
        r = _fence_scan(tmp_path, "da-tools --prometheus http://x widget db-a")
        assert _open(r.findings) == [("V0", "--prometheus", "--prometheus")]


class TestV1UndeclaredFlag:
    @pytest.mark.parametrize("line,token", [
        ("da-tools widget db-a --ci", "--ci"),
        ("da-tools widget --tenant-id db-a", "--tenant-id"),   # not a prefix of --tenant-config
        ("da-tools widget db-a --config x", "--config"),   # ambiguous prefix: argparse rc 2
        ("da-tools widget db-a -x", "-x"),
        ("da-tools widget db-a --period -1d", "-1d"),      # argparse: not a negative number
        ("da-tools widget db-a -vx", "-vx"),               # cluster with an unknown letter
    ])
    def test_a_flag_the_parser_does_not_declare_is_reported(self, tmp_path, line, token):
        found = _open(_fence_scan(tmp_path, line).findings)
        assert ("V1", "widget", token) in found, line
        assert all(v == "V1" for v, _c, _t in found), line

    @pytest.mark.parametrize("line", [
        "da-tools widget db-a --config-dir conf.d/ --json-output",
        "da-tools widget db-a -o out/",
        "da-tools widget db-a -oout/",
        "da-tools widget db-a --output-dir=out/",
        "da-tools widget db-a --prometheus http://localhost:9090",   # injected
        "da-tools widget --help",
        "da-tools widget -h",
        "da-tools widget db-a --rounds -5",          # negative number is a value
        "da-tools widget db-a --rounds -.5",
        "da-tools widget db-a --tags a b c --json-output",
        "da-tools widget db-a -- --not-a-flag",
        "da-tools widget db-a --config-dir <dir> --json-output",
        "da-tools widget db-a --$FLAG",              # placeholder flag, disclosed
        "da-tools widget db-a -vq",                  # short cluster, both store_true
        "da-tools widget db-a -vqo out/",            # cluster ending in a value flag
        "da-tools widget db-a -vqoout/",             # …with the value attached
    ])
    def test_declared_flags_are_not_reported(self, tmp_path, line):
        r = _fence_scan(tmp_path, line)
        assert _open(r.findings) == [], line

    def test_a_placeholder_flag_is_disclosed_not_judged(self, tmp_path):
        r = _fence_scan(tmp_path, "da-tools widget db-a --<flag>")
        assert r.stats["cmd_placeholder_flags"] == 1

    def test_second_level_argparse_subcommands_use_the_child_parser(self, tmp_path):
        clean = _fence_scan(tmp_path, "da-tools tool2 --repo r snapshot --message hi",
                            "da-tools tool2 log --limit 3")
        assert _open(clean.findings) == []
        wrong = _fence_scan(tmp_path, "da-tools tool2 snapshot --limit 3")
        assert _open(wrong.findings) == [("V1", "tool2", "--limit")], (
            "`--limit` belongs to `log`; after `snapshot` argparse hands every "
            "token to the snapshot parser, which rejects it")
        unknown = _fence_scan(tmp_path, "da-tools tool2 purge --message x")
        assert _open(unknown.findings) == [("V1", "tool2", "purge")]

    def test_the_action_slot_counts_positional_tokens_not_positionals(self, tmp_path):
        clean = _fence_scan(tmp_path, "da-tools tool3 a b snapshot --message hi")
        assert _open(clean.findings) == []
        wrong = _fence_scan(tmp_path, "da-tools tool3 a b snapshot --limit 3")
        assert _open(wrong.findings) == [("V1", "tool3", "--limit")]

    def test_an_attached_short_value_does_not_swallow_the_next_token(self, tmp_path):
        """`-rX snapshot`: the value is attached, so `snapshot` is still the
        action and `--message` is judged against the child parser."""
        r = _fence_scan(tmp_path, "da-tools tool2 -rX snapshot --message hi")
        assert _open(r.findings) == []

    def test_the_word_after_an_undeclared_flag_is_its_value_not_a_second_finding(
            self, tmp_path):
        """`--repo r snapshot` under a parser without `--repo`: one finding, not
        `--repo` PLUS `r is not an action`. The skipped word is disclosed."""
        r = _fence_scan(tmp_path, "da-tools tool3 a b --bogus r snapshot --message hi")
        assert _open(r.findings) == [("V1", "tool3", "--bogus")]
        assert r.stats["cmd_skipped_values"] == 1

    def test_a_dispatcher_without_a_parser_is_disclosed_not_judged(self, tmp_path):
        r = _fence_scan(tmp_path, "da-tools noparser bogus --whatever")
        assert _open(r.findings) == []
        assert r.stats["cmd_no_parser"] == 1


class TestV2PrefixAbbreviation:
    @pytest.mark.parametrize("line,token", [
        ("da-tools widget db-a --output out.yaml", "--output"),
        ("da-tools widget db-a --output=out.yaml", "--output"),
        ("da-tools widget db-a --json", "--json"),        # store_true target: still red
        ("da-tools widget db-a --tenant-conf x.yaml", "--tenant-conf"),
    ])
    def test_a_unique_long_prefix_is_reported(self, tmp_path, line, token):
        r = _fence_scan(tmp_path, line)
        assert _open(r.findings) == [("V2", "widget", token)], line

    @pytest.mark.parametrize("line", [
        "da-tools widget db-a --output-dir out/",
        "da-tools widget db-a --json-output",
        "da-tools widget db-a --tenant-config x.yaml",
    ])
    def test_the_full_spelling_is_not_reported(self, tmp_path, line):
        r = _fence_scan(tmp_path, line)
        assert _open(r.findings) == [], line

    def test_the_abbreviation_consumes_the_targets_value(self, tmp_path):
        r = _fence_scan(tmp_path, "da-tools widget db-a --output out.yaml --json-output")
        assert _open(r.findings) == [("V2", "widget", "--output")]

    def test_the_message_says_the_value_and_prose_change_too(self, tmp_path):
        """`--output foo.yaml` → `--output-dir foo.yaml` is green and still wrong
        (a directory named foo.yaml), so the message must say more than the spelling."""
        r = _fence_scan(tmp_path, "da-tools widget db-a --output out.yaml")
        msg = r.findings[0].message
        assert "--output-dir" in msg and "directory" in msg and "#1514" in msg
        ref = _reference(tmp_path, "| `--output <FILE>` | out | stdout |\n")
        row = _scan(tmp_path, reference=ref).findings[0]
        assert row.verdict == "V2" and "directory" in row.message


class TestCommandForms:
    @pytest.mark.parametrize("image", [
        "ghcr.io/vencil/da-tools:v2.9.0",
        "ghcr.io/vencil/da-tools:latest",
        "ghcr.io/vencil/da-tools@sha256:0123456789abcdef",
        "da-tools:dev",
        "mirror.corp/team/da-tools:v2.9.0",
    ])
    def test_docker_run_image_references_reach_the_judgement(self, tmp_path, image):
        r = _fence_scan(
            tmp_path, f"docker run --rm -v $(pwd)/conf.d:/data:ro {image} widget db-a --ci")
        assert _open(r.findings) == [("V1", "widget", "--ci")], image

    @pytest.mark.parametrize("line", [
        "docker tag ghcr.io/vencil/da-tools:v2.9.0 mirror.corp/da-tools:v2.9.0",
        "docker push mirror.corp/da-tools:v2.9.0",
        "docker pull ghcr.io/vencil/da-tools:latest",
        "kind load docker-image da-tools:dev --name cluster",
        "image: ghcr.io/vencil/da-tools:v2.9.0",
    ])
    def test_an_image_reference_that_is_not_run_is_not_a_subject(self, tmp_path, line):
        r = _fence_scan(tmp_path, line)
        assert _open(r.findings) == [], line
        assert r.stats["cmd_segments"] == 0, line

    def test_a_prompt_before_a_subshell_is_not_a_substitution(self, tmp_path):
        r = _fence_scan(tmp_path, "$ (cd x && da-tools widget db-a --ci)")
        assert _open(r.findings) == [("V1", "widget", "--ci")]
        r = _fence_scan(tmp_path, "$ da-tools widget $(pwd) --ci")
        assert _open(r.findings) == [("V1", "widget", "--ci")] and r.stats["cmd_positionals"] == 1

    @pytest.mark.parametrize("line", [
        "docker run --rm -v $(pwd):/work da-tools widget db-a --ci",
        "docker run --name t -p 8080:80 da-tools widget db-a --ci",
        "podman run -w /work da-tools widget db-a --ci",
    ])
    def test_a_bare_image_name_after_run_is_the_image(self, tmp_path, line):
        """A locally built image is run under its bare name; the values of
        the docker options before it are not command words."""
        r = _fence_scan(tmp_path, line)
        assert _open(r.findings) == [("V1", "widget", "--ci")], line
        assert r.stats["cmd_run_without_image"] == 0 and r.stats["cmd_outside_command_position"] == 0

    def test_an_entrypoint_override_is_disclosed_not_judged(self, tmp_path):
        r = _fence_scan(
            tmp_path, "docker run --entrypoint python3 ghcr.io/vencil/da-tools:v2.9.0 x.py --ci")
        assert _open(r.findings) == []
        assert r.stats["cmd_entrypoint_override"] == 1
        # `--entrypoint sh <image> -c "…"` is the shell-string form instead
        r = _fence_scan(
            tmp_path, 'docker run --entrypoint sh ghcr.io/vencil/da-tools:v2.9.0 -c "ls"')
        assert r.stats["cmd_entrypoint_override"] == 0 and r.stats["cmd_sh_c_strings"] == 1

    def test_docker_flags_before_the_image_are_not_judged(self, tmp_path):
        r = _fence_scan(
            tmp_path, "docker run --rm --network=host --user $(id -u):$(id -g) -e X=1 "
                      "ghcr.io/vencil/da-tools:v2.9.0 widget db-a --json-output")
        assert _open(r.findings) == []
        assert r.stats["cmd_segments"] == 1 and r.stats["cmd_run_without_image"] == 0

    @pytest.mark.parametrize("line", [
        "python3 scripts/tools/ops/widget.py db-a --ci",
        "python scripts/tools/ops/widget.py db-a --ci",
        "py -3 scripts/tools/ops/widget.py db-a --ci",
        "python3 -X utf8 scripts/tools/ops/widget.py db-a --ci",
        'python3 "$TOOLS_DIR/ops/widget.py" db-a --ci',
        "./da-tools widget db-a --ci",
        "$HOME/bin/da-tools widget db-a --ci",
        "docker pull ghcr.io/vencil/da-tools:latest && da-tools widget db-a --ci",
    ])
    def test_python_script_and_path_forms_are_reverse_mapped(self, tmp_path, line):
        r = _fence_scan(tmp_path, line)
        assert _open(r.findings) == [("V1", "widget", "--ci")], line

    def test_a_script_outside_command_map_is_disclosed_not_judged(self, tmp_path):
        r = _fence_scan(tmp_path, "python3 scripts/tools/ops/elsewhere.py --ci")
        assert _open(r.findings) == []
        assert r.stats["cmd_script_not_in_map"] == 1

    def test_backslash_continuation_is_one_command(self, tmp_path):
        r = _fence_scan(tmp_path, "docker run --rm \\",
                        "  ghcr.io/vencil/da-tools:v2.9.0 \\",
                        "  widget db-a \\", "  --ci")
        assert [(x.verdict, x.token, x.line) for x in r.findings] == [("V1", "--ci", 2)], (
            "one finding, reported at the first line of the wrapped command")

    @pytest.mark.parametrize("line", [
        "da-tools widget db-a --json-output | kubectl apply -f -",
        "da-tools widget db-a --json-output && kubectl apply -f x",
        "da-tools widget db-a --json-output; ls -la",
        "da-tools widget db-a --json-output 2>&1 | tee -a log",
        "da-tools widget db-a --json-output > out.json",
        "da-tools widget db-a --tags $(cat tags.txt) --json-output",   # $(…) is a value
        "da-tools widget $(cat tenant.txt):$(date +%s) --json-output",
    ])
    def test_flags_after_a_pipe_belong_to_the_next_command(self, tmp_path, line):
        r = _fence_scan(tmp_path, line)
        assert _open(r.findings) == [], line

    def test_a_flag_after_a_redirect_still_belongs_to_the_command(self, tmp_path):
        r = _fence_scan(tmp_path, "da-tools widget db-a > out.json --ci")
        assert _open(r.findings) == [("V1", "widget", "--ci")]

    def test_a_flag_after_a_command_substitution_is_still_judged(self, tmp_path):
        """`$(jq …)` does not end the command."""
        r = _fence_scan(tmp_path, "da-tools widget $(cat tenant.txt) --bogus-flag")
        assert _open(r.findings) == [("V1", "widget", "--bogus-flag")]

    def test_every_command_on_a_line_is_judged(self, tmp_path):
        r = _fence_scan(tmp_path,
                        "da-tools widget db-a --json-output; da-tools widget db-b --ci",
                        "cd conf.d && da-tools widget db-c --bogus",
                        "(cd x && da-tools widget db-d --bogus2) | tee log")
        assert _open(r.findings) == [("V1", "widget", "--ci"), ("V1", "widget", "--bogus"),
                                     ("V1", "widget", "--bogus2")]
        assert r.stats["cmd_segments"] == 4

    @pytest.mark.parametrize("body", [
        _fence("$ da-tools widget db-a --ci"),
        "```yaml\nsteps:\n  - run: da-tools widget db-a --ci\n```\n",
        "```yaml\nscript:\n  - da-tools widget db-a --ci\n```\n",
        "> ```bash\n> da-tools widget db-a --ci\n> ```\n",
        "~~~bash\nda-tools widget db-a --ci\n~~~\n",
        "```bash\nkubectl exec deploy/x -- da-tools widget db-a --ci\n```\n",
        "```bash\nPROMETHEUS_URL=http://x da-tools widget db-a --ci\n```\n",
        "```bash\nsudo -E da-tools widget db-a --ci\n```\n",
        "```bash\ntime da-tools widget db-a --ci\n```\n",
        "```bash\nls conf.d | xargs -I{} da-tools widget {} --ci\n```\n",
        "```bash\ndocker compose run --rm da-tools widget db-a --ci\n```\n",
        "```bash\ncat <<'EOF' > run.sh\nda-tools widget db-a --ci\nEOF\n```\n",
        "```bash\nsh -c \"da-tools widget db-a --ci\"\n```\n",
        "```bash\ndocker run --rm img bash -c 'da-tools widget db-a --ci'\n```\n",
    ])
    def test_command_position_forms_are_scanned(self, tmp_path, body):
        r = _scan(tmp_path, docs=[_doc(tmp_path, body)])
        assert _open(r.findings) == [("V1", "widget", "--ci")], body

    @pytest.mark.parametrize("line,words", [
        ("docker build -t da-tools .", 1),
        ("helm upgrade --install da-tools ./charts/da-tools --set image.tag=v2.9.0", 2),
        ("kubectl logs -n monitoring job/da-tools --tail 50", 0),   # not a da-tools word
        ("- name: da-tools", 1),
        ("echo da-tools --bogus", 1),
    ])
    def test_a_da_tools_word_outside_command_position_is_not_a_subject(
            self, tmp_path, line, words):
        r = _fence_scan(tmp_path, line)
        assert _open(r.findings) == [], line
        assert r.stats["cmd_segments"] == 0, line
        assert r.stats["cmd_bare_not_command"] == words, line
        assert r.stats["cmd_outside_command_position"] == 0, line

    @pytest.mark.parametrize("line", [
        "docker run --rm $IMAGE widget db-a --ci",
        "kubectl run x --image=ghcr.io/vencil/da-tools:v2.9.0 --restart=Never -- widget db-a --ci",
    ])
    def test_a_run_whose_image_is_not_recognisable_is_disclosed(self, tmp_path, line):
        r = _fence_scan(tmp_path, line)
        assert _open(r.findings) == [], line
        assert r.stats["cmd_run_without_image"] == 1, line

    def test_a_run_with_a_recognised_image_is_not_counted_as_unrecognised(self, tmp_path):
        r = _fence_scan(tmp_path, "docker run --rm ghcr.io/vencil/da-tools:v1 widget db-a")
        assert r.stats["cmd_run_without_image"] == 0 and r.stats["cmd_segments"] == 1

    @pytest.mark.parametrize("body", [
        _fence("containers:", "  - name: v", "    image: ghcr.io/vencil/da-tools:v2.9.0",
               '    args: ["widget", "db-a", "--ci"]', lang="yaml"),
        _fence("containers:", "  - name: v", "    image: ghcr.io/vencil/da-tools:v2.9.0",
               '    command: ["da-tools"]', "    args:", "      - widget", "      - db-a",
               '      - "--ci"', lang="yaml"),
        _fence("containers:", "  - name: v", "    image: busybox",
               '    command: ["da-tools", "widget"]', "    args: [db-a, --ci]", lang="yaml"),
        _fence("containers:", "  - name: v", "    image: busybox",
               '    command: ["sh", "-c", "da-tools widget db-a --ci"]', lang="yaml"),
        _fence("containers:", "  - name: v", "    image: busybox",
               '    command: ["da-tools", "widget", "db-a", "--ci"]', lang="yaml"),
        # an `env:` list between image and args is not a container boundary
        _fence("containers:", "  - name: v", "    image: ghcr.io/vencil/da-tools:v2.9.0",
               "    env:", "      - name: PROMETHEUS_URL", "        value: http://p",
               '    args: ["widget", "db-a", "--ci"]', lang="yaml"),
    ])
    def test_manifest_command_and_args_lists_are_expanded_to_argv(self, tmp_path, body):
        r = _scan(tmp_path, docs=[_doc(tmp_path, body)])
        assert _open(r.findings) == [("V1", "widget", "--ci")], body
        assert r.stats["cmd_manifest_argvs"] == 1

    def test_manifest_args_of_a_non_datools_container_are_not_judged(self, tmp_path):
        body = _fence("containers:", "  - name: v", "    image: busybox",
                      '    args: ["widget", "db-a", "--ci"]', lang="yaml")
        r = _scan(tmp_path, docs=[_doc(tmp_path, body)])
        assert _open(r.findings) == [] and r.stats["cmd_manifest_argvs"] == 0
        # a second container in the same pod does not inherit the first's image
        body = _fence("containers:", "  - name: a", "    image: ghcr.io/vencil/da-tools:v1",
                      "  - name: b", "    image: busybox",
                      '    args: ["widget", "db-a", "--ci"]', lang="yaml")
        r = _scan(tmp_path, docs=[_doc(tmp_path, body)])
        assert _open(r.findings) == [] and r.stats["cmd_manifest_argvs"] == 0

    def test_prose_is_not_scanned_but_inline_spans_are(self, tmp_path):
        r = _scan(tmp_path, docs=[_doc(
            tmp_path, "Run `da-tools widget db-a --ci` in CI.\n\nda-tools widget --ci\n"
                      "| step | `da-tools widget db-a --bogus` |\n")])
        assert _open(r.findings) == [("V1", "widget", "--ci"), ("V1", "widget", "--bogus")]
        assert r.stats["cmd_segments"] == 0 and r.stats["inline_spans"] == 2

    def test_inline_spans_without_a_subject_are_not_counted(self, tmp_path):
        r = _scan(tmp_path, docs=[_doc(tmp_path, "Use `--ci` with `conf.d/`.\n")])
        assert r.stats["inline_spans"] == 0 and _open(r.findings) == []

    def test_shell_comments_are_stripped_but_quotes_are_respected(self, tmp_path):
        r = _fence_scan(tmp_path, "da-tools widget db-a --json-output  # --ci is not a flag here",
                        "da-tools widget 'db#a' --json-output")
        assert _open(r.findings) == []

    def test_an_unbalanced_quote_is_disclosed_not_guessed(self, tmp_path):
        r = _fence_scan(tmp_path, 'da-tools widget db-a --ci "it\'s')
        assert _open(r.findings) == []
        assert r.stats["cmd_unparseable"] == 1

    def test_symlinked_docs_are_not_scanned(self, tmp_path):
        (tmp_path / "docs").mkdir()
        real = tmp_path / "docs" / "real.md"
        real.write_text(_fence("da-tools widget db-a --ci"), encoding="utf-8")
        (tmp_path / "docs" / "link.md").symlink_to(real)
        docs, _missing = mod.doc_files(tmp_path)
        assert [d.name for d in docs] == ["real.md"]


class TestInlineIgnore:
    def test_an_ignore_with_a_reason_suppresses_and_is_counted(self, tmp_path):
        r = _fence_scan(
            tmp_path, "da-tools widget db-a --ci  # datools-cmd-ignore: future flag, see RFC")
        assert _open(r.findings) == []
        assert [x.ignored for x in r.findings] == ["future flag, see RFC"]
        assert r.stats["ignored"] == 1
        assert r.errors == []

    @pytest.mark.parametrize("comment", ["# datools-cmd-ignore", "# datools-cmd-ignore:",
                                         "# datools-cmd-ignore:   "])
    def test_an_ignore_without_a_reason_is_a_hard_error(self, tmp_path, comment):
        r = _fence_scan(tmp_path, f"da-tools widget db-a --ci  {comment}")
        assert any("without a reason" in e for e in r.errors), r.errors
        assert r.findings == [], "the line is neither judged nor silently exempted"

    def test_a_prose_line_ignore_covers_its_spans_once(self, tmp_path):
        r = _scan(tmp_path, docs=[_doc(
            tmp_path, "See `da-tools profile build` and `da-tools export` "
                      "<!-- datools-cmd-ignore: planned CLI, not shipped -->\n")])
        assert _open(r.findings) == []
        assert {x.ignored for x in r.findings} == {"planned CLI, not shipped"}
        assert r.stats["ignored"] == 1 and r.errors == []
        bad = _scan(tmp_path, docs=[_doc(
            tmp_path, "See `da-tools profile build` <!-- datools-cmd-ignore -->\n")])
        assert any("without a reason" in e for e in bad.errors)

    @pytest.mark.parametrize("body", [
        _fence("da-tools widget db-a  # datools-cmd-ignore: whatever"),
        "plain prose <!-- datools-cmd-ignore: whatever -->\n",
        "see `da-tools widget db-a` <!-- datools-cmd-ignore: whatever -->\n",
    ])
    def test_an_ignore_that_exempts_nothing_is_stale(self, tmp_path, body):
        """The same debt as a ledger row whose count is too high: the site
        was fixed and the exemption stayed."""
        r = _scan(tmp_path, docs=[_doc(tmp_path, body)])
        assert r.findings == []
        assert [e for e in r.errors if "stale" in e] == [
            f"doc.md:{'2' if body.startswith('```') else '1'}: stale `datools-cmd-ignore` — "
            f"no finding on this line to exempt; remove the marker"], r.errors
        live = _fence_scan(tmp_path, "da-tools widget db-a --ci  # datools-cmd-ignore: whatever")
        assert live.errors == [] and live.stats["ignored"] == 1

    def test_a_reference_row_ignore_is_stale_only_when_no_carrier_uses_it(self, tmp_path):
        ref = _reference(tmp_path, "| `--ci` | x | false <!-- datools-cmd-ignore: planned --> |\n")
        r = _scan(tmp_path, docs=[ref], reference=ref)
        assert _open(r.findings) == [] and r.errors == [], (
            "the prose carrier sees no da-tools in `--ci`; the table carrier exempts the V3")
        clean = _reference(tmp_path, "| `--config-dir` | x | . <!-- datools-cmd-ignore: planned --> |\n")
        r = _scan(tmp_path, docs=[clean], reference=clean)
        assert r.findings == [] and any("stale" in e for e in r.errors)

    def test_a_table_row_ignore_needs_a_reason_too(self, tmp_path):
        ref = _reference(tmp_path, "| `--ci` | x | false | <!-- datools-cmd-ignore -->\n")
        assert any("without a reason" in e for e in _scan(tmp_path, reference=ref).errors)
        ref2 = _reference(tmp_path, "| `--ci` | x | false | <!-- datools-cmd-ignore: not shipped -->\n")
        r = _scan(tmp_path, reference=ref2)
        assert r.errors == [] and _open(r.findings) == [] and r.stats["ignored"] == 1


class TestV3OptionTable:
    def test_a_phantom_row_is_reported(self, tmp_path):
        ref = _reference(tmp_path, "| `--ci` | CI mode | false |\n")
        assert _open(_scan(tmp_path, reference=ref).findings) == [("V3", "widget", "--ci")]

    def test_an_abbreviated_row_is_v2_not_v3(self, tmp_path):
        ref = _reference(tmp_path, "| `--output <FILE>` | out | stdout |\n")
        assert _open(_scan(tmp_path, reference=ref).findings) == [("V2", "widget", "--output")]

    @pytest.mark.parametrize("row", [
        "| `-o/--output-dir <PATH>` | out | - |\n",       # both spellings judged
        "| `-o, --output-dir <PATH>` | out | - |\n",
        "| `--format md\\|json` | fmt | md |\n",          # escaped pipe stays one cell
        "| `--prometheus <URL>` | injected | - |\n",
        "| `--help` | help | - |\n",
        "| `<tenant>` | positional | - |\n",
    ])
    def test_declared_rows_are_not_reported(self, tmp_path, row):
        r = _scan(tmp_path, reference=_reference(tmp_path, _REF_CLEAN_ROW + row))
        assert _open(r.findings) == [] and r.errors == [] and r.fatal == [], row

    def test_a_flag_row_under_an_unknown_header_is_a_hard_error(self, tmp_path):
        ref = tmp_path / "ref.md"
        ref.write_text("#### widget\n\n| 旗標 | 敘述 |\n|---|---|\n| `--ci` | x |\n" + _REF_EXIT,
                       encoding="utf-8")
        r = _scan(tmp_path, reference=ref)
        assert any("unrecognised table header" in e and "旗標" in e for e in r.errors), r.errors

    def test_prose_mentioning_a_flag_under_an_unknown_header_is_not_an_error(self, tmp_path):
        ref = tmp_path / "ref.md"
        ref.write_text("#### widget\n\n| 輸出 | 內容 |\n|---|---|\n"
                       "| `--json` 的 `coverage[]` | one entry per silence |\n"
                       + _REF_HEAD.split("\n", 2)[2] + _REF_CLEAN_ROW + _REF_EXIT,
                       encoding="utf-8")
        assert _scan(tmp_path, reference=ref).errors == []

    def test_an_unknown_table_does_not_inherit_the_previous_tables_kind(self, tmp_path):
        """A separator under an unpinned header ends the previous table's reading."""
        ref = tmp_path / "ref.md"
        ref.write_text(_REF_HEAD + _REF_CLEAN_ROW + "\n| 模式 | 說明 |\n|---|---|\n"
                       "| `strict` | x |\n" + _REF_EXIT, encoding="utf-8")
        r = _scan(tmp_path, reference=ref)
        assert r.errors == [] and ("模式", "說明") not in r.by_header

    def test_a_section_ends_at_a_higher_heading(self, tmp_path):
        ref = tmp_path / "ref.md"
        ref.write_text(_REF_HEAD + _REF_CLEAN_ROW + _REF_EXIT
                       + "\n## Appendix\n\n| 旗標 | 敘述 |\n|---|---|\n| `--ci` | x |\n",
                       encoding="utf-8")
        assert _scan(tmp_path, reference=ref).errors == [], "a table after `##` is not widget's"

    def test_a_heading_naming_no_command_ends_the_section_and_is_counted(self, tmp_path):
        """`#### Rollback 程序` is not a command; a table under it must not be
        judged as the previous command's, and the heading is disclosed."""
        ref = tmp_path / "ref.md"
        ref.write_text(_REF_HEAD + _REF_CLEAN_ROW + _REF_EXIT
                       + "\n#### Rollback 程序\n\n| 選項 | 說明 | 預設值 |\n|---|---|---|\n"
                       "| `--dirs <d>` | other tool's flag | y |\n", encoding="utf-8")
        r = _scan(tmp_path, reference=ref)
        assert _open(r.findings) == [] and r.errors == []
        assert r.stats["reference_sections_unmatched"] == 1
        control = _scan(tmp_path, reference=_reference(tmp_path))
        assert control.stats["reference_sections_unmatched"] == 0

    def test_a_fenced_comment_inside_a_section_is_not_a_heading(self, tmp_path):
        """A `# comment` inside an example block does not end the command's section."""
        ref = tmp_path / "ref.md"
        ref.write_text(_REF_HEAD + _REF_CLEAN_ROW + "\n```bash\n# comment\nda-tools widget x\n```\n"
                       + _REF_EXIT.replace("| `3` | blocked |\n", ""), encoding="utf-8")
        assert _open(_scan(tmp_path, reference=ref).findings) == [("V4", "widget", "3")]


class TestV4ExitCodeTable:
    def test_reachable_codes_resolve(self):
        ec = mod.reachable_exit_codes(_WIDGET_SOURCE)
        assert set(ec.reachable) == {0, 1, 2, 3}
        assert ec.undecidable == []

    def test_an_opaque_expression_is_undecidable(self):
        ec = mod.reachable_exit_codes(_OPAQUE_SOURCE)
        assert ec.reachable == {} and ec.undecidable == [3]

    def test_a_nested_functions_return_is_not_mains(self):
        src = ("import sys\n\ndef main():\n    def helper():\n        return 7\n"
               "    f = lambda: 9\n    helper()\n    return 0\n\nsys.exit(main())\n")
        ec = mod.reachable_exit_codes(src)
        assert set(ec.reachable) == {0}, ec
        control = mod.reachable_exit_codes(
            "import sys\n\ndef main():\n    return 7\n\nsys.exit(main())\n")
        assert set(control.reachable) == {7}

    def test_a_reachable_code_missing_from_the_table_is_reported(self, tmp_path):
        ref = _reference(tmp_path, exit_table=_REF_EXIT.replace("| `3` | blocked |\n", "")
                         .replace("| `2` | caller |\n", ""))
        assert _open(_scan(tmp_path, reference=ref).findings) == [
            ("V4", "widget", "2"), ("V4", "widget", "3")]

    def test_a_complete_table_is_not_reported(self, tmp_path):
        assert _open(_scan(tmp_path, reference=_reference(tmp_path)).findings) == []

    def test_several_codes_in_one_cell_are_each_documented(self, tmp_path):
        table = ("\n**結束碼**\n\n| 代碼 | 說明 |\n|------|------|\n"
                 "| `0` | ok |\n| `1` / `2` | bad |\n| 3 | blocked |\n")
        r = _scan(tmp_path, reference=_reference(tmp_path, exit_table=table))
        assert _open(r.findings) == [] and r.per_doc["ref.md"]["exit_codes"] == 3

    def test_zero_and_documented_but_unreachable_codes_are_not_judged(self, tmp_path):
        table = _REF_EXIT + "| `9` | never |\n"
        r = _scan(tmp_path, reference=_reference(tmp_path, exit_table=table),
                  exit_codes={"widget": mod.reachable_exit_codes("import sys\nsys.exit(1)\n")})
        assert _open(r.findings) == []

    def test_argparse_makes_2_reachable_even_when_the_source_never_spells_it(self, tmp_path):
        table = "\n**結束碼**\n\n| 代碼 | 說明 |\n|------|------|\n| `0` | ok |\n| `1` | x |\n"
        r = _scan(tmp_path, reference=_reference(tmp_path, exit_table=table),
                  exit_codes={"widget": mod.reachable_exit_codes("import sys\nsys.exit(1)\n")})
        assert _open(r.findings) == [("V4", "widget", "2")]

    def test_an_all_opaque_script_is_disclosed_not_judged(self, tmp_path):
        table = "\n**結束碼**\n\n| 代碼 | 說明 |\n|------|------|\n| `0` | ok |\n"
        r = _scan(tmp_path, reference=_reference(tmp_path, exit_table=table),
                  exit_codes={"widget": mod.reachable_exit_codes(_OPAQUE_SOURCE)})
        assert _open(r.findings) == [] and r.stats["exit_undecidable_scripts"] == 1

    def test_disclosure_counts_are_per_command_not_per_document(self, tmp_path):
        """A script seen in both reference docs is one script."""
        opaque = {"widget": mod.reachable_exit_codes(_OPAQUE_SOURCE)}
        table = "\n**結束碼**\n\n| 代碼 | 說明 |\n|------|------|\n| `0` | ok |\n"
        a = _reference(tmp_path, exit_table=table, name="a.md")
        b = _reference(tmp_path, exit_table=table, name="b.md")
        r = mod.scan(parsers=PARSERS, command_map=COMMAND_MAP, docs=[],
                     reference_docs=(a, b), injected=INJECTED, exit_codes=opaque,
                     repo_root=tmp_path)
        assert r.stats["exit_undecidable_scripts"] == 1

    def test_a_section_with_a_parser_but_no_exit_table_is_disclosed(self, tmp_path):
        ref = tmp_path / "ref.md"
        ref.write_text(_REF_HEAD + _REF_CLEAN_ROW + "\n#### tool2\n\n| 選項 | 說明 | 預設值 |\n"
                       "|---|---|---|\n| `--repo <r>` | r | - |\n" + _REF_EXIT.replace(
                           "**結束碼**", "#### widget\n\n**結束碼**"), encoding="utf-8")
        r = _scan(tmp_path, reference=ref)
        assert r.stats["commands_without_exit_table"] == 1      # tool2, not widget
        assert _scan(tmp_path, reference=_reference(tmp_path)).stats[
            "commands_without_exit_table"] == 0

    def test_the_exit_header_shapes_are_pinned(self):
        """Which table headers produce exit-code comparisons (the shared pin)."""
        assert mod._EXIT_HEADERS == {
            ("代碼", "說明"), ("Code", "Description"), ("Code", "意義"),
            ("Code", "含義"), ("Code", "Meaning"), ("Exit Code", "含義", "CI 行為"),
        }


class TestBaselineLedger:
    def _entry(self, **kw):
        base = dict(file="doc.md", command="widget", verdict="V1", token="--ci",
                    count=1, ticket="#1380")
        base.update(kw)
        return mod.BaselineEntry(**base)

    def test_an_entry_suppresses_exactly_count_findings(self, tmp_path):
        r = _fence_scan(tmp_path, "da-tools widget db-a --ci", "da-tools widget db-b --ci")
        open_, suppressed, errors = mod.apply_baseline(r.findings, [self._entry(count=2)])
        assert open_ == [] and len(suppressed) == 2 and errors == []

    def test_a_surplus_finding_with_a_ledgered_key_stays_open_and_names_every_site(
            self, tmp_path):
        """The open one is the later site, but the NEW one may be the earlier
        line, so the message lists every line with this key."""
        r = _fence_scan(tmp_path, "da-tools widget db-a --ci", "da-tools widget db-b --ci")
        open_, suppressed, errors = mod.apply_baseline(r.findings, [self._entry(count=1)])
        assert [f.line for f in open_] == [3] and len(suppressed) == 1 and errors == []
        assert "2 of 2 with this key" in open_[0].message and "lines 2, 3" in open_[0].message \
            and "(#1380) covers 1 of them" in open_[0].message
        # the new site sits BEFORE the ledgered one
        r = _fence_scan(tmp_path, "da-tools widget NEW --ci", "echo", "echo", "echo",
                        "da-tools widget db-a --ci")
        open_, _s, _e = mod.apply_baseline(r.findings, [self._entry(count=1)])
        assert [f.line for f in open_] == [6] and "lines 2, 6" in open_[0].message
        # control: a key the ledger covers exactly carries no annotation
        r = _fence_scan(tmp_path, "da-tools widget db-a --ci", "da-tools widget db-b --ci")
        open_, suppressed, _e = mod.apply_baseline(r.findings, [self._entry(count=2)])
        assert open_ == [] and not any("with this key" in f.message for f in suppressed)

    def test_fewer_findings_than_count_is_stale(self, tmp_path):
        r = _fence_scan(tmp_path, "da-tools widget db-a --ci")
        _o, _s, errors = mod.apply_baseline(r.findings, [self._entry(count=2)])
        assert any("stale" in e and "count is 2" in e for e in errors), errors

    def test_a_stale_entry_is_a_hard_error(self, tmp_path):
        r = _fence_scan(tmp_path, "da-tools widget db-a --json-output")
        _o, _s, errors = mod.apply_baseline(r.findings, [self._entry()])
        assert any("stale" in e for e in errors), errors

    def test_a_finding_exempted_twice_is_a_hard_error_but_one_each_is_fine(self, tmp_path):
        """Per FINDING, not per key: one site in the ledger and another under
        an inline ignore is one exemption each."""
        both = _fence_scan(tmp_path, "da-tools widget db-a --ci",
                           "da-tools widget db-b --ci  # datools-cmd-ignore: why")
        open_, suppressed, errors = mod.apply_baseline(both.findings, [self._entry(count=1)])
        assert errors == [] and open_ == [] and len(suppressed) == 1
        only = _fence_scan(tmp_path, "da-tools widget db-a --ci  # datools-cmd-ignore: why")
        _o, _s, errors = mod.apply_baseline(only.findings, [self._entry(count=1)])
        assert any("exemption per finding" in e for e in errors), errors
        assert not any("stale" in e for e in errors), "double exemption, not staleness"

    @pytest.mark.parametrize("ticket", ["#TODO", "1380", "TRK-370", ""])
    def test_a_placeholder_ticket_is_rejected(self, tmp_path, ticket):
        p = tmp_path / "b.yaml"
        p.write_text("entries:\n  - {file: d.md, command: widget, verdict: V1, "
                     f"token: \"--ci\", count: 1, ticket: \"{ticket}\"}}\n", encoding="utf-8")
        _e, errors = mod.load_baseline(p)
        assert any("must be `#<number>`" in e for e in errors), errors

    @pytest.mark.parametrize("count", ["0", "-1", "x", "true"])
    def test_a_bad_count_is_rejected(self, tmp_path, count):
        p = tmp_path / "b.yaml"
        p.write_text("entries:\n  - {file: d.md, command: widget, verdict: V1, "
                     f"token: \"--ci\", count: {count}, ticket: \"#1\"}}\n", encoding="utf-8")
        assert any("count" in e for e in mod.load_baseline(p)[1])

    def test_a_well_formed_ledger_loads_cleanly(self, tmp_path):
        p = tmp_path / "b.yaml"
        p.write_text("entries:\n  - {file: d.md, command: widget, verdict: V1, "
                     "token: \"--ci\", count: 2, ticket: \"#1380\"}\n", encoding="utf-8")
        entries, errors = mod.load_baseline(p)
        assert errors == [] and [(e.ticket, e.count) for e in entries] == [("#1380", 2)]

    def test_duplicates_and_bad_verdicts_are_rejected(self, tmp_path):
        p = tmp_path / "b.yaml"
        row = '  - {file: d.md, command: widget, verdict: %s, token: "--ci", count: 1, ticket: "#1"}\n'
        p.write_text("entries:\n" + row % "V1" + row % "V1" + row % "V9", encoding="utf-8")
        _e, errors = mod.load_baseline(p)
        assert any("duplicate" in e for e in errors) and any("V9" in e for e in errors)

    def test_a_missing_or_malformed_ledger_is_a_hard_error(self, tmp_path):
        assert mod.load_baseline(tmp_path / "nope.yaml")[1]
        p = tmp_path / "b.yaml"
        p.write_text("entries: {not: a list}\n", encoding="utf-8")
        assert mod.load_baseline(p)[1]
        p.write_text("entries:\n  - {file: d.md, command: widget, verdict: V1, "
                     "token: \"--ci\", ticket: \"#1\"}\n", encoding="utf-8")
        assert any("count" in e for e in mod.load_baseline(p)[1]), "count is mandatory"

    def test_write_baseline_escapes_tokens(self, tmp_path):
        """A backslash in a token must survive the YAML round trip."""
        r = _fence_scan(tmp_path, "da-tools 'foo\\bar' --x", "da-tools 'a\"b'")
        out = tmp_path / "b.yaml"
        mod.write_baseline(r.findings, [], out)
        entries, _errors = mod.load_baseline(out)
        assert {e.token for e in entries} == {"foo\\bar", 'a"b'}
        open_, suppressed, errors = mod.apply_baseline(
            r.findings, [e._replace(ticket="#1") for e in entries])
        assert open_ == [] and errors == [] and len(suppressed) == 2

    def test_write_baseline_round_trips_counts_and_keeps_known_tickets(self, tmp_path):
        r = _fence_scan(tmp_path, "da-tools widget db-a --ci", "da-tools widget db-b --ci",
                        "da-tools widget db-a --output x")
        out = tmp_path / "b.yaml"
        assert mod.write_baseline(r.findings, [self._entry()], out) == 2
        entries, errors = mod.load_baseline(out)
        rows = {e.key(): (e.count, e.ticket) for e in entries}
        assert rows[("doc.md", "widget", "V1", "--ci")] == (2, "#1380")
        assert rows[("doc.md", "widget", "V2", "--output")] == (1, "#TODO")
        assert any("#TODO" in e for e in errors), "a fresh row must not pass as-is"


class TestBlindIsNotClean:
    def test_an_empty_command_map_is_fatal(self, tmp_path):
        r = _scan(tmp_path, parsers={}, command_map={})
        assert any("zero commands" in e for e in r.fatal), r.fatal

    def test_zero_judged_tokens_is_fatal(self, tmp_path):
        ref = tmp_path / "ref.md"
        ref.write_text("#### widget\n\nnothing\n", encoding="utf-8")
        r = _scan(tmp_path, reference=ref)
        assert r.stats["scored"] == 0
        assert any("compared nothing" in e for e in r.fatal), r.fatal

    def test_a_reference_doc_with_no_judged_rows_is_fatal_per_carrier(self, tmp_path):
        r = _scan(tmp_path, reference=_reference(tmp_path, exit_table=""))
        assert any("0 judged exit codes" in e for e in r.fatal), r.fatal
        r = _scan(tmp_path, reference=_reference(tmp_path, rows=""))
        assert any("0 judged option-table flags" in e for e in r.fatal), r.fatal

    def test_a_missing_landing_page_is_reported(self, tmp_path):
        (tmp_path / "docs").mkdir()
        _docs, missing = mod.doc_files(tmp_path)
        assert missing == list(mod.EXTRA_DOC_FILES)

    def test_the_scan_surface_identity_is_the_shared_one(self):
        """The invariant is imported from check_cli_default_drift, not rewritten."""
        assert mod._scan_surface_faults({"a": "a.py"}, {}, [], []) != []
        assert mod._scan_surface_faults({"a": "a.py"}, {"a": {}}, [], []) == []

    def _run_main(self, monkeypatch, findings, errors, fatal, argv, ledger=([], [])):
        stats = mod._new_stats()
        stats["scored"] = 1
        monkeypatch.setattr(mod, "scan", lambda *a, **k: mod.ScanResult(
            findings, stats, errors, fatal, {}, {}))
        monkeypatch.setattr(mod, "load_baseline", lambda *a, **k: ledger)
        monkeypatch.setattr(sys, "argv", [str(SCRIPT)] + argv)
        return mod.main()

    def test_a_finding_reaches_the_process_exit_code(self, monkeypatch):
        finding = mod.Finding("V1", "docs/x.md", 1, "widget", "--ci", "msg")
        assert self._run_main(monkeypatch, [finding], [], [], ["--ci"]) == 1
        assert self._run_main(monkeypatch, [finding], [], [], []) == 0, (
            "without --ci the finding is printed, not fatal")
        assert self._run_main(monkeypatch, [], [], [], ["--ci"]) == 0, "control"

    def test_a_content_error_is_rc_1_under_ci(self, monkeypatch):
        assert self._run_main(monkeypatch, [], ["stale baseline entry …"], [], ["--ci"]) == 1
        assert self._run_main(monkeypatch, [], ["stale baseline entry …"], [], []) == 0

    def test_an_apparatus_failure_is_rc_2_with_or_without_ci(self, monkeypatch):
        """A gate that could not run must not look like a gate that found
        something (rc 1) or nothing (rc 0) — dev-rules #13 caller-error."""
        for argv in (["--ci"], []):
            assert self._run_main(monkeypatch, [], [], ["could not load x"], argv) == 2
            assert self._run_main(monkeypatch, [], [], [], argv,
                                  ledger=([], ["baseline ledger is missing"])) == 2

    def test_write_baseline_refuses_when_the_scan_did_not_complete(self, monkeypatch, tmp_path,
                                                                 capsys):
        """A ledger regenerated under a blind parser would silently drop
        every row of that command."""
        target = tmp_path / "b.yaml"
        monkeypatch.setattr(mod, "BASELINE_PATH", target)
        monkeypatch.setattr(mod, "write_baseline",
                            lambda f, e, path=target: path.write_text("entries: []\n") or 0)
        assert self._run_main(monkeypatch, [], [], ["could not load foo"],
                              ["--write-baseline"]) == 2
        assert not target.exists(), "refused means nothing written"
        assert "[FATAL]" in capsys.readouterr().err
        assert self._run_main(monkeypatch, [], [], [], ["--write-baseline"]) == 0, "control"
        assert target.exists()

    def test_json_mode_emits_exactly_one_document(self, monkeypatch, capsys):
        import json
        assert self._run_main(monkeypatch, [], [], [], ["--json"]) == 0
        doc = json.loads(capsys.readouterr().out)
        assert set(doc) == {"findings", "suppressed", "ignored", "stats", "errors", "fatal"}


class TestSubstitutionsExemptionsAndShells:
    """Substitution, wrapper, shell-string, manifest, fence and ignore-marker
    shapes: each with a positive and a negative."""

    @pytest.mark.parametrize("line", [
        "da-tools widget $(pwd); da-tools widget db-a --ci",            # `);` glued
        "da-tools widget $(pwd)|| da-tools widget db-a --ci",           # `)||` glued
        "da-tools widget $(dirname $(pwd)) --ci",                        # nested `))`
        "da-tools widget $((1+2)) --ci",                                 # arithmetic `((`
        "da-tools widget db-a --config-dir $(pwd)/conf.d && da-tools widget db-b --ci",
    ])
    def test_substitutions_glued_to_operators_or_nested_do_not_hide_the_rest(self, tmp_path, line):
        assert _open(_fence_scan(tmp_path, line).findings) == [("V1", "widget", "--ci")], line

    def test_an_unbalanced_substitution_is_unparseable_not_silent(self, tmp_path):
        r = _fence_scan(tmp_path, "da-tools widget $(cat t --ci")
        assert _open(r.findings) == [] and r.stats["cmd_unparseable"] == 1

    @pytest.mark.parametrize("line", [
        "if da-tools widget db-a --ci; then echo ok; fi",
        "if ! da-tools widget db-a --ci; then exit 1; fi",
        "{ da-tools widget db-a --ci; }",
        "while da-tools widget db-a --ci; do sleep 1; done",
        "env FOO=bar da-tools widget db-a --ci",
        "docker run -e X=1 da-tools widget db-a --ci",       # bare local image name
    ])
    def test_reserved_words_and_assignment_wrappers_are_command_positions(self, tmp_path, line):
        assert _open(_fence_scan(tmp_path, line).findings) == [("V1", "widget", "--ci")], line

    @pytest.mark.parametrize("line", [
        "docker exec vibe-dev-container da-tools widget db-a --ci",
        "timeout 30 da-tools widget db-a --ci",
        "xargs -n 1 da-tools widget --ci",
        "sudo -u vibe da-tools widget db-a --ci",
        'docker exec vibe-dev-container sh -c "da-tools widget db-a --ci"',
        'docker exec -it vibe-dev-container bash -lc "da-tools widget db-a --ci"',
        "│ da-tools widget --ci → deploy │",                   # box diagram
        "[ ] 6. Sample: da-tools widget <id> --ci",            # checklist item
    ])
    def test_an_invocation_outside_command_position_is_disclosed_in_its_own_bucket(
            self, tmp_path, line):
        r = _fence_scan(tmp_path, line)
        assert _open(r.findings) == [] and r.stats["cmd_segments"] == 0, line
        assert r.stats["cmd_outside_command_position"] == 1, line
        assert r.stats["cmd_bare_not_command"] == 0, line
        assert r.stats["cmd_sh_c_strings"] == 0, line

    @pytest.mark.parametrize("line", [
        'bash --norc -c "da-tools widget db-a --ci"',
        'sh -x -c "da-tools widget db-a --ci"',
        'bash -o pipefail -c "da-tools widget db-a --ci"',
        'bash -c -- "da-tools widget db-a --ci"',
        'docker run --rm --entrypoint bash ghcr.io/vencil/da-tools:v1 -e -c "da-tools widget db-a --ci"',
    ])
    def test_the_shell_string_follows_the_shells_other_options(self, tmp_path, line):
        r = _fence_scan(tmp_path, line)
        assert _open(r.findings) == [("V1", "widget", "--ci")], line
        assert r.stats["cmd_sh_c_strings"] == 1 and r.stats["cmd_unparseable"] == 0, line

    def test_a_c_after_the_end_of_options_is_an_operand(self, tmp_path):
        r = _fence_scan(tmp_path, 'bash -- -c "da-tools widget db-a --ci"')
        assert _open(r.findings) == [] and r.stats["cmd_sh_c_strings"] == 0

    def test_a_da_tools_image_running_a_shell_is_an_unknown_subcommand(self, tmp_path):
        """The image's entrypoint is da-tools, so `sh` is argv[0]."""
        r = _fence_scan(tmp_path, 'docker run --rm ghcr.io/vencil/da-tools:latest sh -c "ls"')
        assert _open(r.findings) == [("V0", "sh", "sh")] and r.stats["cmd_sh_c_strings"] == 0
        assert r.stats["cmd_outside_command_position"] == 0, "no da-tools inside `ls`"
        r = _fence_scan(tmp_path, 'docker run --rm ghcr.io/vencil/da-tools:latest sh -c '
                                  '"da-tools widget db-a --ci"')
        assert _open(r.findings) == [("V0", "sh", "sh")] and r.stats["cmd_sh_c_strings"] == 0
        assert r.stats["cmd_outside_command_position"] == 1, "the string's da-tools is disclosed"
        for line in ('docker run --rm --entrypoint sh ghcr.io/vencil/da-tools:latest '
                     '-c "da-tools widget db-a --ci"',
                     'docker run --rm busybox sh -c "da-tools widget db-a --ci"',
                     'docker run --rm $IMAGE sh -c "da-tools widget db-a --ci"'):
            runs = _fence_scan(tmp_path, line)
            assert _open(runs.findings) == [("V1", "widget", "--ci")], line
            assert runs.stats["cmd_sh_c_strings"] == 1, line

    @pytest.mark.parametrize("line", [
        'bash -lc "da-tools widget db-a --ci"',
        'sh -ec "da-tools widget db-a --ci"',
        'bash -cl "da-tools widget db-a --ci"',        # bash reads the cluster in any order
        'kubectl exec pod -- sh -c "da-tools widget db-a --ci"',
    ])
    def test_short_flag_clusters_containing_c_are_shell_strings(self, tmp_path, line):
        r = _fence_scan(tmp_path, line)
        assert _open(r.findings) == [("V1", "widget", "--ci")] and r.stats["cmd_sh_c_strings"] == 1

    def test_a_shell_option_without_c_is_not_a_command_string(self, tmp_path):
        r = _fence_scan(tmp_path, 'sh -x "da-tools widget db-a --ci"')
        assert _open(r.findings) == [] and r.stats["cmd_sh_c_strings"] == 0

    @pytest.mark.parametrize("line", [
        "sh -c 'da-tools widget db-a --ci \"x'",     # inner string unparseable
        "sh -c",                                       # no string at all
    ])
    def test_a_broken_shell_string_is_counted_unparseable(self, tmp_path, line):
        r = _fence_scan(tmp_path, line)
        assert _open(r.findings) == [] and r.stats["cmd_unparseable"] == 1, line

    @pytest.mark.parametrize("body,expect", [
        (_fence("containers:", "  - name: v", "    image: busybox",
                '    command: ["/bin/sh", "-c"]', '    args: ["da-tools widget db-a --ci"]',
                lang="yaml"), [("V1", "widget", "--ci")]),
        (_fence("services:", "  tool:", "    image: ghcr.io/vencil/da-tools:v2.9.0",
                '    command: ["widget", "db-a", "--ci"]', lang="yaml"),
         [("V1", "widget", "--ci")]),
        (_fence("services:", "  tool:", "    image: ghcr.io/vencil/da-tools:v2.9.0",
                "    command:", "    - widget", "    - db-a", '    - "--ci"', lang="yaml"),
         [("V1", "widget", "--ci")]),                                  # items at key indent
        (_fence("services:", "  tool:", "    image: ghcr.io/vencil/da-tools:v2.9.0",
                "    entrypoint: da-tools", '    command: ["widget", "db-a", "--ci"]',
                lang="yaml"), [("V1", "widget", "--ci")]),
        (_fence("services:", "  tool:", "    image: ghcr.io/vencil/da-tools:v2.9.0",
                '    command: ["sh", "-c", "da-tools widget db-a --ci"]', lang="yaml"),
         [("V1", "widget", "--ci")]),
        (_fence("containers:", "  - name: v", "    image: ghcr.io/vencil/da-tools:v2.9.0",
                '    command: ["python3", "-m", "http.server", "8080"]', lang="yaml"), []),
        (_fence("containers:", "  - name: v", "    image: ghcr.io/vencil/da-tools:v2.9.0",
                '    command: ["widget", "db-a", "--ci"]', lang="yaml"), []),
        (_fence("containers:", "  - name: v", "    image: ghcr.io/vencil/da-tools:v2.9.0",
                '    command: ["python3", "serve.py"]', '    args: ["--ci"]', lang="yaml"), []),
        (_fence("services:", "  tool:", "    image: ghcr.io/vencil/da-tools:v2.9.0",
                '    entrypoint: ["python3"]', '    command: ["x.py", "--ci"]',
                lang="yaml"), []),
    ])
    def test_manifest_shapes_follow_their_schemas_semantics(self, tmp_path, body, expect):
        """k8s `command:` REPLACES the entrypoint (a da-tools image serving
        http.server runs no da-tools); compose `command:` is the CMD under the
        da-tools entrypoint unless `entrypoint:` replaces it. The overrides are
        disclosed, not judged."""
        r = _scan(tmp_path, docs=[_doc(tmp_path, body)])
        assert _open(r.findings) == expect, body
        assert r.stats["cmd_manifest_argvs"] == (1 if expect else 0)
        assert r.stats["cmd_entrypoint_override"] == (0 if expect else 1), body

    @pytest.mark.parametrize("body", [
        _fence("containers:", "  - name: v", "    image: ghcr.io/vencil/da-tools:v1  # pinned",
               '    args: ["widget", "db-a", "--ci"]', lang="yaml"),
        _fence("containers:", "  - name: v", "    image: ghcr.io/vencil/da-tools:v1",
               '    args: ["widget", "db-a", "--ci"]  # see the flag table', lang="yaml"),
        _fence("containers:", "  - name: v", "    image: busybox",
               '    command: ["sh", "-c", "da-tools widget db-a --ci"]  # note', lang="yaml"),
        _fence("containers:", "  - name: v", "    image: ghcr.io/vencil/da-tools:v1",
               "    args:", "      - widget", "      - db-a", '      - "--ci"  # note',
               lang="yaml"),
    ])
    def test_a_yaml_comment_does_not_change_what_a_manifest_line_declares(self, tmp_path, body):
        r = _scan(tmp_path, docs=[_doc(tmp_path, body)])
        assert _open(r.findings) == [("V1", "widget", "--ci")], body
        assert r.stats["cmd_manifest_argvs"] == 1 and r.errors == []

    def test_a_manifest_finding_is_exempted_on_the_line_it_points_at(self, tmp_path):
        head = ("containers:", "  - name: v", "    image: ghcr.io/vencil/da-tools:v1")
        on_args = _fence(*head, '    args: ["widget", "db-a", "--ci"]  # datools-cmd-ignore: planned',
                         lang="yaml")
        r = _scan(tmp_path, docs=[_doc(tmp_path, on_args)])
        assert _open(r.findings) == [] and r.stats["ignored"] == 1 and r.errors == []
        assert [f.ignored for f in r.findings] == ["planned"]
        on_command = _fence(*head, '    command: ["da-tools", "widget", "db-a", "--ci"]  '
                                   "# datools-cmd-ignore: planned", lang="yaml")
        r = _scan(tmp_path, docs=[_doc(tmp_path, on_command)])
        assert _open(r.findings) == [] and r.stats["ignored"] == 1 and r.errors == []
        on_item = _fence(*head, "    args:", "      - widget", "      - db-a",
                         '      - "--ci"  # datools-cmd-ignore: planned', lang="yaml")
        r = _scan(tmp_path, docs=[_doc(tmp_path, on_item)])
        assert _open(r.findings) == [("V1", "widget", "--ci")], "an item line exempts nothing"
        assert "`args:` / `command:` line this finding points at" in r.findings[0].message
        assert r.findings[0].line == 5, "the finding points at the args: line"
        on_image = _fence("containers:", "  - name: v",
                          "    image: ghcr.io/vencil/da-tools:v1  # datools-cmd-ignore: planned",
                          '    args: ["widget", "db-a", "--ci"]', lang="yaml")
        r = _scan(tmp_path, docs=[_doc(tmp_path, on_image)])
        assert _open(r.findings) == [("V1", "widget", "--ci")]
        assert any("stale" in e for e in r.errors), "a marker on the image line exempts nothing"
        no_reason = _fence(*head, '    args: ["widget", "db-a", "--ci"]  # datools-cmd-ignore',
                           lang="yaml")
        r = _scan(tmp_path, docs=[_doc(tmp_path, no_reason)])
        assert any("without a reason" in e for e in r.errors) and r.findings == []

    @pytest.mark.parametrize("body", [
        _fence("services:", "  tool:", '    entrypoint: ["/bin/sh"]',
               "    image: ghcr.io/vencil/da-tools:v1",
               '    command: ["-c", "da-tools widget db-a --ci"]', lang="yaml"),
        _fence("services:", "  tool:", '    entrypoint: ["/bin/sh", "-c"]',
               "    image: ghcr.io/vencil/da-tools:v1",
               '    command: ["da-tools widget db-a --ci"]', lang="yaml"),
        _fence("containers:", "  - name: v", '    args: ["widget", "db-a", "--ci"]',
               "    image: ghcr.io/vencil/da-tools:v1", lang="yaml"),
        _fence("services:", "  tool:", "    image: ghcr.io/vencil/da-tools:v1",
               "    command: widget db-a --ci", lang="yaml"),
        _fence("containers:", "  - name: v", "    image: ghcr.io/vencil/da-tools:v1",
               "    args: [", '      "widget",', '      "db-a",', '      "--ci"', "    ]",
               lang="yaml"),
        _fence("containers:", "  - image: ghcr.io/vencil/da-tools:v1", "    name: v",
               '    args: ["widget", "db-a", "--ci"]', lang="yaml"),
    ])
    def test_a_containers_keys_are_judged_together_whatever_their_order(self, tmp_path, body):
        """compose runs entrypoint + command and k8s runs command + args, so
        the process is known only once every key of the container is in."""
        r = _scan(tmp_path, docs=[_doc(tmp_path, body)])
        assert _open(r.findings) == [("V1", "widget", "--ci")], body
        assert r.stats["cmd_manifest_argvs"] == 1 and r.stats["cmd_entrypoint_override"] == 0

    def test_a_manifest_site_is_judged_by_one_carrier_only(self, tmp_path):
        body = _fence("containers:", "  - name: v", "    image: ghcr.io/vencil/da-tools:v1",
                      '    command: ["/bin/sh", "-c"]', "    args:",
                      "      - da-tools widget db-a --ci", lang="yaml")
        r = _scan(tmp_path, docs=[_doc(tmp_path, body)])
        assert [(f.verdict, f.line, f.token) for f in r.findings] == [("V1", 6, "--ci")]
        assert r.stats["cmd_manifest_argvs"] == 1 and r.stats["cmd_segments"] == 1
        literal = _fence("containers:", "  - name: v", "    image: ghcr.io/vencil/da-tools:v1",
                         '    command: ["sh", "-c"]', "    args:", "      - |",
                         "        da-tools widget db-a --ci", lang="yaml")
        r = _scan(tmp_path, docs=[_doc(tmp_path, literal)])
        assert [(f.verdict, f.line, f.token) for f in r.findings] == [("V1", 8, "--ci")], (
            "a `- |` string is judged where it is written, by the fenced-line carrier")
        assert r.stats["cmd_manifest_argvs"] == 0 and r.stats["cmd_entrypoint_override"] == 0

    def test_a_diff_fence_judges_only_added_lines(self, tmp_path):
        body = "```diff\n- da-tools widget db-a --old-flag\n+ da-tools widget db-a --ci\n```\n"
        r = _scan(tmp_path, docs=[_doc(tmp_path, body)])
        assert _open(r.findings) == [("V1", "widget", "--ci")]
        body = "```diff\n-da-tools widget db-a --old-flag\n+da-tools widget db-a --ci\n```\n"
        assert _open(_scan(tmp_path, docs=[_doc(tmp_path, body)]).findings) == [
            ("V1", "widget", "--ci")]
        control = "```bash\n- da-tools widget db-a --old-flag\n```\n"
        assert _open(_scan(tmp_path, docs=[_doc(tmp_path, control)]).findings) == [
            ("V1", "widget", "--old-flag")], "outside a diff fence `- ` is a list bullet"

    def test_a_longer_fence_keeps_inner_fences_as_content(self, tmp_path):
        body = "````markdown\n```bash\nda-tools widget db-a --ci\n```\n````\n"
        r = _scan(tmp_path, docs=[_doc(tmp_path, body)])
        assert _open(r.findings) == [("V1", "widget", "--ci")]
        body = "```bash\nda-tools widget db-a --ci\n```\n\nprose `da-tools widget db-a --bogus`\n"
        r = _scan(tmp_path, docs=[_doc(tmp_path, body)])
        assert _open(r.findings) == [("V1", "widget", "--ci"), ("V1", "widget", "--bogus")], (
            "control: an equal-length fence still closes")

    def test_two_adjacent_equal_fences_are_two_blocks(self, tmp_path):
        body = "```yaml\ncontainers:\n- name: a\n  image: ghcr.io/vencil/da-tools:v1\n```\n" \
               "```yaml\n  args: [widget, --ci]\n```\n"
        r = _scan(tmp_path, docs=[_doc(tmp_path, body)])
        assert _open(r.findings) == [] and r.stats["cmd_manifest_argvs"] == 0, (
            "the second block's args attached to the first block's image")
        assert len(mod._fence_blocks(body.splitlines())) == 2
        joined = body.replace("```\n```yaml\n", "")
        assert _open(_scan(tmp_path, docs=[_doc(tmp_path, joined)]).findings) == [
            ("V1", "widget", "--ci")], "control: in ONE block the args do attach"

    @pytest.mark.parametrize("comment", [
        "# see datools-cmd-ignore in docs",          # prose about the marker
        "# datools-cmd-ignored stuff",               # another word
    ])
    def test_a_comment_that_merely_mentions_the_marker_is_not_an_ignore(self, tmp_path, comment):
        r = _fence_scan(tmp_path, f"da-tools widget db-a --ci  {comment}")
        assert _open(r.findings) == [("V1", "widget", "--ci")] and r.stats["ignored"] == 0
        assert r.errors == []
        r = _fence_scan(tmp_path, "da-tools widget db-a --ci  # datools-cmd-ignore: real reason")
        assert _open(r.findings) == [] and r.stats["ignored"] == 1

    def test_a_marker_without_its_colon_has_no_reason(self, tmp_path):
        r = _fence_scan(tmp_path, "da-tools widget db-a --ci  # datools-cmd-ignore reason")
        assert any("without a reason" in e for e in r.errors) and r.findings == []
        prose = _scan(tmp_path, docs=[_doc(
            tmp_path, "See `da-tools widget db-a --ci` <!-- datools-cmd-ignore reason -->\n")])
        assert any("without a reason" in e for e in prose.errors) and prose.findings == []
        mention = _scan(tmp_path, docs=[_doc(
            tmp_path, "See `da-tools widget db-a --ci` <!-- see datools-cmd-ignore -->\n")])
        assert _open(mention.findings) == [("V1", "widget", "--ci")] and mention.errors == []
        ref = _reference(tmp_path, "| `--ci` | x | false <!-- datools-cmd-ignore reason --> |\n")
        assert any("without a reason" in e for e in _scan(tmp_path, reference=ref).errors)

    def test_a_table_row_ignore_inside_the_last_cell_is_honoured(self, tmp_path):
        r = _scan(tmp_path, docs=[_doc(
            tmp_path, "| a | `da-tools profile build` planned <!-- datools-cmd-ignore: not shipped --> |\n")])
        assert _open(r.findings) == [] and r.stats["ignored"] == 1 and r.errors == []


@pytest.fixture(scope="module")
def result():
    r = mod.scan()
    entries, ledger_errors = mod.load_baseline()
    open_, suppressed, baseline_errors = mod.apply_baseline(r.findings, entries)
    return dict(findings=r.findings, stats=r.stats, errors=r.errors + baseline_errors,
                fatal=r.fatal + ledger_errors, by_header=r.by_header, per_doc=r.per_doc,
                entries=entries, open=open_, suppressed=suppressed)


class TestRealRepo:
    """The shipped tree, the shipped ledger, and probes that keep the green honest."""

    def test_the_tree_is_green_under_the_ledger(self, result):
        assert result["fatal"] == [], "\n".join(result["fatal"])
        assert result["errors"] == [], "\n".join(result["errors"])
        assert result["open"] == [], "\n".join(
            f"{f.verdict} {f.file}:{f.line} {f.command} {f.token}" for f in result["open"])

    def test_the_probes_are_measured(self, result):
        """The gate must SEE the defects the content tickets describe, or a
        green tree proves only that it looked at nothing."""
        keys = {(f.verdict, f.command, f.token) for f in result["findings"]}
        for probe in [("V1", "validate-config", "--ci"),        # #1380
                      ("V1", "onboard", "--analyze"),           # #1381
                      ("V2", "onboard", "--output"),            # #1514 class (abbreviation)
                      ("V3", "lint", "--strict"),               # #1619
                      ("V1", "shadow-verify", "--window")]:     # #1513, inline span only
            assert probe in keys, f"probe {probe} not measured; the whole run is void"
        assert any(f.file == "docs/schemas/migration-state.md" for f in result["findings"]), (
            "the inline-span carrier stopped seeing migration-state.md (#1381)")

    @pytest.mark.parametrize("command,flag,pattern", [
        ("offboard", "--config-dir", r"offboard[^\n]*--config-dir"),
        ("analyze-gaps", "--tenant-config", r"analyze-gaps[^\n]*--tenant-config"),
        ("drift-detect", "--ci", r"drift-detect[^\n]*--ci\b"),
        ("guard", "defaults-impact", r"guard defaults-impact"),
    ])
    def test_the_negative_probes_exist_in_the_docs_and_are_not_reported(
            self, result, command, flag, pattern):
        docs, _missing = mod.doc_files()
        assert any(re.search(pattern, d.read_text(encoding="utf-8")) for d in docs), (
            f"{command} {flag} is no longer in any scanned doc, so this negative "
            f"control proves nothing — pick a live one")
        assert not any(f.command == command and f.token == flag
                       for f in result["findings"]), f"false red on {command} {flag}"

    def test_every_ledger_row_names_a_real_ticket_and_is_live(self, result):
        assert result["entries"], "an empty ledger on this tree means nothing loaded"
        assert all(mod._TICKET_RE.match(e.ticket) for e in result["entries"])
        live = {}
        for f in result["suppressed"]:
            live[f.key()] = live.get(f.key(), 0) + 1
        assert {e.key(): e.count for e in result["entries"]} == live, (
            "the ledger is not set-equal to the suppressed findings")

    def test_write_baseline_reproduces_the_shipped_ledger(self, result, tmp_path):
        out = tmp_path / "regen.yaml"
        mod.write_baseline(result["findings"], result["entries"], out)
        regen, errors = mod.load_baseline(out)
        assert errors == []
        assert sorted(regen) == sorted(result["entries"]), (
            "--write-baseline and the shipped ledger disagree; regenerate it")
        assert out.read_bytes() == mod.BASELINE_PATH.read_bytes(), (
            "the regenerated text differs from the shipped file; regenerate it")

    def test_the_productive_header_shapes_are_pinned(self, result):
        productive = {k for k, v in result["by_header"].items() if v > 0}
        assert productive == {
            ("選項", "說明", "預設值"), ("參數", "說明"), ("參數", "說明", "預設值"),
            ("參數", "說明", "預設"), ("參數", "用途", "預設"), ("參數", "用途"),
            ("Flag", "Default", "說明"), ("Flag", "Default", "Description"),
            ("Option", "Description", "Default"), ("Parameter", "Description"),
            ("Parameter", "Description", "Default"), ("Argument", "Description", "Default"),
            ("Parameter", "Purpose", "Default"), ("Parameter", "Purpose"),
        }, sorted(productive)
        assert productive <= mod._OPTION_HEADERS

    def test_both_reference_docs_contribute_both_carriers(self, result):
        for rel, counts in result["per_doc"].items():
            assert counts["option_rows"] > 0 and counts["exit_codes"] > 0, (rel, counts)
        assert set(result["per_doc"]) == {"docs/cli-reference.md", "docs/cli-reference.en.md"}

    def test_the_exit_code_probe_resolves(self):
        """config-diff is 0/1/2 by construction; if the AST reader cannot see
        that, every exit-code comparison is void."""
        script = mod._resolve(mod.parse_command_map()["config-diff"])
        ec = mod.reachable_exit_codes(script.read_text(encoding="utf-8"))
        assert {1, 2} <= set(ec.reachable), ec

    def test_the_identity_holds_and_only_the_go_wrappers_are_unscoreable(self):
        cmap = mod.parse_command_map()
        captured, unscoreable, blind, faults = mod.introspect_parsers()
        assert cmap and not faults and not blind, (faults, blind)
        assert len(captured) + len(unscoreable) + len(blind) == len(cmap)
        assert {u.split()[0] for u in unscoreable} == {"guard", "parser", "batch-pr"}

    def test_every_carrier_is_exercised_on_the_real_tree(self, result):
        s = result["stats"]
        assert s["cmd_segments"] and s["inline_spans"] and s["cmd_manifest_argvs"], s
        assert s["cmd_segments"] + s["inline_spans"] >= len(
            {(e.file, e.command) for e in result["entries"] if e.verdict in ("V0", "V1", "V2")})

    def test_the_landing_pages_are_scanned_and_symlinks_are_not(self):
        docs, missing = mod.doc_files()
        assert missing == []
        rels = {d.relative_to(REPO_ROOT).as_posix() for d in docs}
        assert set(mod.EXTRA_DOC_FILES) <= rels
        assert not any("/internal/" in r for r in rels)
        assert (REPO_ROOT / "docs" / "CHANGELOG.md").is_symlink(), "the control lost its object"
        assert "docs/CHANGELOG.md" not in rels


class TestEnvironmentIsScrubbed:
    def test_result_does_not_depend_on_the_ambient_environment(self):
        def run(extra_env):
            env = {**os.environ, **extra_env, "PYTHONIOENCODING": "utf-8"}
            return subprocess.run([sys.executable, "-s", str(SCRIPT), "--json"],
                                  capture_output=True, env=env,
                                  timeout=validate_all.CHECK_TIMEOUT_SECONDS,
                                  cwd=str(REPO_ROOT))
        clean = run({})
        polluted = run({"PROMETHEUS_URL": "http://sentinel.invalid:9090",
                        "TENANT_API_URL": "http://sentinel.invalid",
                        "DA_GOVERN_GROUPS": "sentinel"})
        assert clean.returncode == polluted.returncode == 0, (clean.stderr, polluted.stderr)
        assert clean.stdout == polluted.stdout
        assert b"sentinel.invalid" not in polluted.stdout
