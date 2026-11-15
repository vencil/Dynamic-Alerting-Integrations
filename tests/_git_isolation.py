"""A git config for the test session: the host's, with signing off (#3896).

Tests that create a temporary repository and commit inherit the host's git
config. With the developer's ``commit.gpgsign = true`` (``gpg.format``,
``gpg.ssh.program``, ...) every such commit calls the host's signer: one that
waits on an agent or a prompt stalls the run, and a broken one fails every
commit with "failed to write commit object", which reads as a regression in
the code under test. Test commits never need a signature and CI has no
signer, so the suite must not depend on one.

``write_config_copy`` writes a config file holding every system and global
entry git reads now — in the order it reads them, includes resolved — and
then ``commit.gpgsign = false`` and ``tag.gpgsign = false``, which come last
and win. ``tests/conftest.py`` points ``GIT_CONFIG_GLOBAL`` at it and sets
``GIT_CONFIG_NOSYSTEM`` for the session, so a developer's or CI's own
settings (user.*, safe.directory, filter.lfs.*, ...) keep working and only
signing changes. The Go side does the same for tenant-api's tests
(components/tenant-api/internal/testutil/gitenv.go, #3889).

Why GIT_CONFIG_GLOBAL and not GIT_CONFIG_COUNT / GIT_CONFIG_KEY_n: git strips
the latter from the environment of the receive-pack / upload-pack it spawns
for a local-transport push or fetch, and tests/ops/test_generator_service.py
asserts the GIT_CONFIG_COUNT carrier a test sets itself. A test that sets its
own GIT_CONFIG_GLOBAL (``/dev/null``, a file of its own) still wins: it
replaces this file for its own git processes.
"""
from __future__ import annotations

import os
import subprocess
from typing import Mapping, Optional

#: Written after the copy, so they win over whatever the host config says.
SIGNING_OFF = (("commit.gpgsign", "false"), ("tag.gpgsign", "false"))

CONFIG_FILE_NAME = "gitconfig"


class GitConfigCopyError(RuntimeError):
    """The host's git config could not be copied; the session must not run
    with the host's signing settings instead."""


def _quote(s: str) -> str:
    """*s* as a double-quoted git config value (or subsection name)."""
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n").replace("\t", "\\t")
    return f'"{s}"'


def _split_key(key: str) -> "tuple[str, str, str]":
    """A canonical git config key as (section, subsection or "", name); the
    subsection may hold dots."""
    first, last = key.find("."), key.rfind(".")
    if first <= 0 or last == len(key) - 1:
        raise GitConfigCopyError(f"unexpected git config key {key!r}")
    sub = key[first + 1:last] if last > first else ""
    return key[:first], sub, key[last + 1:]


def _is_include_key(key: str) -> bool:
    # --includes already listed what the includes pulled in; copying the
    # include keys as well would make git follow them again from the copy.
    return key == "include.path" or (
        key.startswith("includeif.") and key.endswith(".path"))


def config_copy_text(workdir: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Every system and global entry git reads under *env* (default:
    os.environ), as config file text, in git's order, includes resolved and
    include keys left out. git lists them from *workdir* with discovery
    stopped above it, so no repository's own config is copied; an existing
    GIT_CONFIG_GLOBAL / GIT_CONFIG_NOSYSTEM is honoured as git honours it.
    The command scope (GIT_CONFIG_COUNT, GIT_CONFIG_PARAMETERS) stays in the
    environment and is not copied."""
    run_env = dict(os.environ if env is None else env)
    run_env["GIT_CEILING_DIRECTORIES"] = os.path.dirname(os.path.abspath(workdir))
    try:
        proc = subprocess.run(
            ["git", "-C", workdir, "config", "--show-scope", "--includes", "-z", "--list"],
            capture_output=True, env=run_env, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitConfigCopyError(f"list the git config to copy: {exc}") from exc
    # rc 1 with no output: no config at all (nothing to copy), not an error.
    if proc.returncode not in (0, 1) or (proc.returncode == 1 and proc.stdout):
        raise GitConfigCopyError(
            f"list the git config to copy: git exited {proc.returncode}: "
            f"{proc.stderr.decode('utf-8', 'replace')}")
    out = proc.stdout.decode("utf-8", "surrogateescape")
    # -z with --show-scope: "<scope>\0<key>\n<value>\0", or "<scope>\0<key>\0"
    # for a key written without "=".
    fields = out.split("\0")
    if len(fields) % 2 != 1 or fields[-1] != "":
        raise GitConfigCopyError(f"unexpected git config listing {out!r}")
    lines = []
    for scope, rest in zip(fields[0:-1:2], fields[1:-1:2]):
        if scope not in ("system", "global"):
            continue
        key, has_value, value = rest.partition("\n")
        if _is_include_key(key):
            continue
        section, sub, name = _split_key(key)
        lines.append(f"[{section} {_quote(sub)}]" if sub else f"[{section}]")
        # Without "=" a key is boolean true; keep it that way, not "true".
        lines.append(f"\t{name} = {_quote(value)}" if has_value else f"\t{name}")
    return "".join(line + "\n" for line in lines)


def write_config_copy(dest_dir: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Write the copy (``config_copy_text``) plus ``SIGNING_OFF`` to
    *dest_dir*/gitconfig and return its path. Before returning, git reads the
    file back: it must parse and say false for both signing keys."""
    text = config_copy_text(dest_dir, env)
    for key, value in SIGNING_OFF:
        section, _sub, name = _split_key(key)
        text += f"[{section}]\n\t{name} = {value}\n"
    path = os.path.join(dest_dir, CONFIG_FILE_NAME)
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as fh:
        fh.write(text)
    os.chmod(path, 0o600)
    for key, value in SIGNING_OFF:
        got = read_key(path, key)
        if got != value:
            raise GitConfigCopyError(
                f"the copied git config {path} says {key}={got!r}, not {value!r}")
    return path


def read_key(path: str, key: str) -> Optional[str]:
    """The value git reads for *key* from the config file *path* (the last
    one, as git uses it), or None when it is unset; a file git cannot parse
    raises GitConfigCopyError."""
    proc = subprocess.run(["git", "config", "--file", path, "--get", key],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False, timeout=60)
    if proc.returncode == 1 and not proc.stderr:
        return None
    if proc.returncode != 0:
        raise GitConfigCopyError(
            f"git cannot read {key} from {path} (rc {proc.returncode}): {proc.stderr}")
    return proc.stdout.rstrip("\n")
