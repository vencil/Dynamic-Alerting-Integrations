"""Shared dispatcher for da-tools subcommands that wrap a Go binary.

v2.8.0 ships three Python dispatchers that each forward to a Go binary
built from components/threshold-exporter/app/cmd/:

  - guard_dispatch.py    → da-guard
  - batchpr_dispatch.py  → da-batchpr
  - parser_dispatch.py   → da-parser

The dispatchers shared ~95% of their code (~270 LOC each, ~810 LOC
total). This module factors that out so each shim is a thin metadata-
only wrapper of ~70 LOC.

Why dispatchers exist at all (vs calling Go binary directly):

  1. da-tools is the user-facing CLI everyone already runs; the Python
     wrapper provides bilingual help, $PATH-fallback binary resolution,
     and consistent error UX across the toolkit.
  2. Subcommand validation happens before subprocess.exec so users get
     a clean Python-side error, not a misleading Go-binary panic.
  3. Each Python dispatcher's docstring explains the design tradeoff
     for that specific tool (e.g., why parser logic stays in Go).

What this module absorbs (config-driven):

  - Bilingual usage printer
  - Binary resolution: --<flag> <path> | $<ENV> | shutil.which(<name>)
  - Subcommand allowlist
  - Friendly missing-binary error with install hints
  - subprocess.run with stdio passthrough
  - Exit code passthrough (binary's rc, or 2 for caller error)

What stays in each shim (intentionally per-tool):

  - Module docstring (per-tool design rationale)
  - Usage block strings (each tool's flag schema is unique)
  - Module-level configuration object
  - main(argv) entry that delegates to dispatcher.dispatch(argv)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import detect_cli_lang  # noqa: E402

# Caller error exit code — bad flag, missing binary, unknown subcommand,
# subprocess OSError. Distinct from the Go binary's exit code (which
# is passed through as-is for 0 / 1 semantics).
_EXIT_CALLER_ERROR = 2


@dataclass
class GoBinaryDispatcher:
    """Config-driven dispatcher for a da-tools subcommand wrapping a Go binary.

    Construct one instance per dispatcher shim, hold it as a module
    constant, and route the shim's main() through dispatch(argv).

    Attributes:
        binary_name: Filename of the Go binary on $PATH or in the image
            (e.g., "da-guard").
        cli_alias: CLI subcommand name under da-tools (e.g., "guard").
            Used in error messages and the install hint URL.
        binary_flag: Flag the user passes to override the binary path
            (e.g., "--da-guard-binary"). Both space-separated and =-form
            (`--flag value` / `--flag=value`) are accepted.
        env_var: Environment variable for binary override
            (e.g., "DA_GUARD_BINARY").
        subcommands: Set of valid subcommands for this dispatcher.
            Validation only — the Go binary owns per-subcommand flag
            schemas; we don't second-guess flag values.
        pass_subcommand: If True (batchpr / parser pattern), forward
            the subcommand as the first arg to the Go binary; the Go
            binary is itself a multi-subcommand dispatcher. If False
            (guard pattern), strip the subcommand before forwarding —
            it's a Python-side organising layer only.
        usage_en: English usage block printed for --help / no-args.
        usage_zh: Traditional Chinese usage block.

    Notes:
        Language detection is deferred to message-build time
        (re-reads $DA_LANG / $LC_ALL / $LANG on every call) so tests
        and shells that toggle DA_LANG mid-process see the change.
    """
    binary_name: str
    cli_alias: str
    binary_flag: str
    env_var: str
    subcommands: set
    pass_subcommand: bool
    usage_en: str
    usage_zh: str

    # ------------------------------------------------------------------
    # Internal helpers — kept private so shims and tests stay decoupled
    # from the message templating; only dispatch() is the public surface.
    # ------------------------------------------------------------------

    def _msg(self, en: str, zh: str) -> str:
        return zh if detect_cli_lang() == "zh" else en

    def _resolve_binary(
        self, args: list[str]
    ) -> tuple[str | None, list[str]]:
        """Return (binary_path_or_None, args_with_binary_flag_stripped).

        Walks args looking for self.binary_flag in either form
        (`--flag value` or `--flag=value`), strips the flag pair, and
        captures the explicit override path. Falls back through env
        var, then $PATH lookup. Trailing bare flag without a value is
        silently dropped — downstream would have rejected it anyway.
        """
        cleaned: list[str] = []
        explicit: str | None = None
        eq_form = self.binary_flag + "="
        i = 0
        while i < len(args):
            a = args[i]
            if a == self.binary_flag:
                if i + 1 >= len(args):
                    i += 1
                    continue
                explicit = args[i + 1]
                i += 2
                continue
            if a.startswith(eq_form):
                explicit = a.split("=", 1)[1]
                i += 1
                continue
            cleaned.append(a)
            i += 1

        if explicit:
            return (
                explicit if os.path.isfile(explicit) else None
            ), cleaned
        env_override = os.environ.get(self.env_var, "").strip()
        if env_override:
            return (
                env_override if os.path.isfile(env_override) else None
            ), cleaned
        return shutil.which(self.binary_name), cleaned

    def _recover_explicit_attempt(
        self, argv: list[str]
    ) -> str | None:
        """Recover the explicit binary path the user attempted (if any),
        for the missing-binary error message. Peeks at the original argv
        before _resolve_binary's stripping pass."""
        eq_form = self.binary_flag + "="
        for i, a in enumerate(argv):
            if a == self.binary_flag and i + 1 < len(argv):
                return argv[i + 1]
            if a.startswith(eq_form):
                return a.split("=", 1)[1]
        return None

    def _print_binary_missing(
        self, explicit_attempt: str | None
    ) -> None:
        """Print friendly missing-binary error to stderr."""
        bn = self.binary_name
        if explicit_attempt:
            print(self._msg(
                f"Error: {bn} binary not found "
                f"at {explicit_attempt!r}.\n",
                f"錯誤: 在 {explicit_attempt!r} 找不到 {bn} 執行檔。\n"
            ), file=sys.stderr)
            return

        print(self._msg(
            f"Error: {bn} binary not found.\n"
            "Resolution order:\n"
            f"  1. {self.binary_flag} <path>\n"
            f"  2. ${self.env_var} env var\n"
            f"  3. {bn} on $PATH\n"
            "\n"
            "Install options:\n"
            "  - Download from "
            "https://github.com/vencil/Dynamic-Alerting-Integrations/releases\n"
            "    (look for tools/v* release assets when v2.8.0 ships)\n"
            "  - Build from source:\n"
            "      cd components/threshold-exporter/app && \\\n"
            f"          go build -o /usr/local/bin/{bn} ./cmd/{bn}\n",
            f"錯誤: 找不到 {bn} 執行檔。\n"
            "解析順序:\n"
            f"  1. {self.binary_flag} <path>\n"
            f"  2. ${self.env_var} 環境變數\n"
            f"  3. PATH 中的 {bn}\n"
            "\n"
            "安裝方式:\n"
            "  - 從 "
            "https://github.com/vencil/Dynamic-Alerting-Integrations/releases"
            " 下載\n"
            "    (v2.8.0 釋出後請看 tools/v* release assets)\n"
            "  - 從原始碼編譯:\n"
            "      cd components/threshold-exporter/app && \\\n"
            f"          go build -o /usr/local/bin/{bn} ./cmd/{bn}\n"
        ), file=sys.stderr)

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def dispatch(self, argv: list[str]) -> int:
        """Dispatch da-tools <cli_alias> args to the Go binary.

        Args:
            argv: Args after the cli_alias word. entrypoint.py drops
                the alias before forwarding here. The first element
                MUST be the subcommand (or a help flag).

        Returns:
            Go binary's exit code on success path. ``2`` on caller
            errors (unknown subcommand, missing binary, OSError during
            exec). Help / no-args returns ``0``.
        """
        if not argv or argv[0] in ("-h", "--help", "help"):
            print(self._msg(self.usage_en, self.usage_zh))
            return 0

        subcmd = argv[0]
        if subcmd not in self.subcommands:
            available = ", ".join(sorted(self.subcommands))
            print(self._msg(
                f"Error: unknown {self.cli_alias} subcommand "
                f"'{subcmd}'.\n"
                f"Available: {available}\n"
                f"Run `da-tools {self.cli_alias} --help` for usage.\n",
                f"錯誤: 未知的 {self.cli_alias} 子命令 '{subcmd}'。\n"
                f"可用子命令: {available}\n"
                f"執行 `da-tools {self.cli_alias} --help` 查看用法。\n"
            ), file=sys.stderr)
            return _EXIT_CALLER_ERROR

        binary, forward_args = self._resolve_binary(argv[1:])
        if binary is None:
            self._print_binary_missing(
                self._recover_explicit_attempt(argv)
            )
            return _EXIT_CALLER_ERROR

        if self.pass_subcommand:
            cmd = [binary, subcmd] + forward_args
        else:
            cmd = [binary] + forward_args

        try:
            # Inherit stdio so output streams straight to the user.
            # No timeout: orchestration across thousands of tenant
            # configs / 10K rules can legitimately take 10+ minutes;
            # user CTRL-C is the contract.
            result = subprocess.run(cmd, check=False)  # subprocess-timeout: ignore
            return result.returncode
        except FileNotFoundError:
            # Race: binary disappeared between resolve and exec. Treat
            # as missing.
            self._print_binary_missing(binary)
            return _EXIT_CALLER_ERROR
        except OSError as e:
            print(self._msg(
                f"Error: failed to execute "
                f"{self.binary_name}: {e}\n",
                f"錯誤: 執行 {self.binary_name} 失敗: {e}\n"
            ), file=sys.stderr)
            return _EXIT_CALLER_ERROR
