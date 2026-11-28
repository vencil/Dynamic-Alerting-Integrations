"""zizmor: one pin, run by name in CI, three audits on (#3756 ③c, owner 2026-11-28).

zizmor audits this repository's GitHub Actions setup. It is wired the way
actionlint is: the upstream pre-commit hook (``zizmorcore/zizmor-pre-commit``),
pinned by its ``rev:`` in ``.pre-commit-config.yaml``, and the CI ``Lint`` job
runs THAT hook by name — so there is one version, and the laptop and CI run the
same one. Which audits run is ``.github/zizmor.yml``.

What this file holds, by reading the files (no zizmor, no network):

* the pin is the hook's ``rev:`` and nothing else installs zizmor — no
  requirement line for it, no installer or runner naming it (pip, pipx, uv,
  cargo, brew, …), no container image, no ``zizmor-action`` (a second copy is
  the drift ``toolchain-versions.md`` exists to prevent). Spelled out, so an
  installer it does not name is not seen;
* ``ci.yml``'s Lint job runs ``pre-commit run zizmor --all-files``;
* the hook, exactly: offline (no token, no network, the same answer
  everywhere), the regular persona, inline ignores off, the config file, the
  one exclusion — no argument more (one could turn the gate off);
* the config turns off audits only, and never one of the three the owner
  turned on (``excessive-permissions``, ``overprovisioned-secrets``,
  ``secrets-inherit``);
* ``toolchain-versions.md`` has a zizmor row naming the pin and this file.

⚠️ What it does NOT see: whether every OTHER audit is really off. zizmor
accepts a rule name it does not know without a word, so a wrong name turns
nothing off — and the audit it meant then runs and reddens the hook. That
failure is loud, in the hook itself; this file does not try to know zizmor's
audit list.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from _tree import repo_files

_REPO = Path(__file__).resolve().parents[2]
_PRE_COMMIT = _REPO / ".pre-commit-config.yaml"
_CI_YML = _REPO / ".github" / "workflows" / "ci.yml"
_CONFIG = _REPO / ".github" / "zizmor.yml"
_TOOLCHAIN = _REPO / "docs" / "internal" / "toolchain-versions.md"
_HOOK_REPO = "https://github.com/zizmorcore/zizmor-pre-commit"
#: The audits the owner turned on (2026-11-28). The config may not turn them off.
_ON = frozenset({"excessive-permissions", "overprovisioned-secrets", "secrets-inherit"})

# A second way of getting zizmor (blind review widened it): an installer or
# runner naming it on the same line, its container image, its action, or a
# requirement line for it in a requirements / constraints file.
_OTHER_INSTALL = re.compile(
    r"(?:\bpip3?|\bpipx|\buvx?|\buv\s+tool|\bcargo|\bbrew|\bnix|\bapt(?:-get)?|\bconda)\b[^\n]*\bzizmor\b"
    r"|ghcr\.io/zizmorcore/|zizmorcore/zizmor-action|\bzizmor-action\b",
    re.IGNORECASE)
_REQUIREMENT_LINE = re.compile(r"^\s*zizmor\b", re.IGNORECASE | re.MULTILINE)
#: The hook, exactly: the arguments and the one exclusion. An extra argument
#: (`--no-exit-codes`, `--min-severity=high`) or a wider `exclude:` would turn
#: the gate off while every required argument is still there (blind review).
_ARGS = ["--no-progress", "--offline", "--persona=regular", "--no-ignores",
         "--config", ".github/zizmor.yml"]
_EXCLUDE = r"^components/threshold-exporter/app/third_party/yaml\.v3/"


def _hook_entry() -> dict:
    cfg = yaml.safe_load(_PRE_COMMIT.read_text(encoding="utf-8"))
    entries = [r for r in cfg["repos"] if str(r.get("repo", "")).rstrip("/") == _HOOK_REPO]
    assert len(entries) == 1, f"expected one {_HOOK_REPO} entry, found {len(entries)}"
    return entries[0]


def test_the_hook_is_pinned_to_an_exact_release():
    rev = str(_hook_entry()["rev"])
    assert re.fullmatch(r"v\d+\.\d+\.\d+", rev), f"zizmor hook rev {rev!r} is not an exact release"


def test_nothing_else_installs_zizmor():
    """The hook's `rev` is the only pin: any other install is a second version."""
    found = []
    for path in repo_files():
        rel = path.relative_to(_REPO).as_posix()
        if rel == "tests/shared/test_zizmor_pin.py" or rel.startswith("docs/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in _OTHER_INSTALL.finditer(text):
            found.append(f"{rel}: {m.group(0)!r}")
        if re.search(r"(requirements|constraints)[^/]*\.(txt|in)$", rel):
            for m in _REQUIREMENT_LINE.finditer(text):
                found.append(f"{rel}: requirement line {m.group(0).strip()!r}")
    assert not found, ("zizmor is installed somewhere other than the pre-commit hook "
                       "(its `rev` is the one pin):\n  " + "\n  ".join(found))


def test_ci_runs_the_hook_by_name():
    jobs = yaml.safe_load(_CI_YML.read_text(encoding="utf-8"))["jobs"]
    runs = "\n".join(str(step.get("run", "")) for step in jobs["lint"]["steps"])
    assert re.search(r"^\s*pre-commit run zizmor --all-files\s*$", runs, re.MULTILINE), (
        "ci.yml's Lint job must run `pre-commit run zizmor --all-files`: it runs hooks "
        "by name, so a hook it does not name is gated by nothing in CI")


def test_the_hook_is_exactly_the_reviewed_one():
    """Offline (the same answer everywhere), the regular persona, inline
    ignores off, the config — and nothing more: no other argument, no other
    key that changes which files it sees or whether it fails."""
    (hook,) = [h for h in _hook_entry()["hooks"] if h["id"] == "zizmor"]
    assert set(hook) == {"id", "args", "exclude"}, sorted(hook)
    assert [str(a) for a in hook["args"]] == _ARGS, hook["args"]
    assert hook["exclude"] == _EXCLUDE, hook["exclude"]


def test_the_config_only_turns_audits_off_and_never_the_three():
    cfg = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    assert set(cfg) == {"rules"}, f"unexpected top-level keys: {sorted(cfg)}"
    rules = cfg["rules"]
    not_off = {name: rule for name, rule in rules.items() if rule != {"disable": True}}
    assert not not_off, f"a rule that is not a plain `disable: true`: {not_off}"
    assert not (_ON & set(rules)), f"turned off, but the owner turned them on: {sorted(_ON & set(rules))}"


def test_toolchain_versions_names_the_pin_and_this_guard():
    rows = [line for line in _TOOLCHAIN.read_text(encoding="utf-8").splitlines()
            if line.startswith("| zizmor ")]
    assert len(rows) == 1, f"expected one zizmor row in toolchain-versions.md, found {len(rows)}"
    assert ".pre-commit-config.yaml" in rows[0] and "test_zizmor_pin.py" in rows[0], rows[0]
