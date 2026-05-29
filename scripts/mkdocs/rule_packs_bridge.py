"""mkdocs hook — materialize repo-root ``rule-packs/*.md`` into ``docs/rule-packs/`` for
the build, symlink-free and cross-platform.

Replaces the former ``docs/rule-packs -> ../rule-packs`` git symlink. That symlink
materialized as a 13-byte text file on Windows (``core.symlinks=false``), so
``mkdocs build`` could not traverse it and emitted ~32 ``rule-packs/* not found``
warnings locally that were green on Linux CI (where the symlink resolved) — forcing
``MKDOCS_STRICT_BYPASS=1`` pushes. Copying the markdown in ``on_pre_build`` (before
mkdocs' file discovery and the static-i18n plugin run) lets the files be processed
natively — including i18n ``.en.md`` pairing — exactly as the symlink used to, on
every platform, so the local strict build now matches CI.

Why a pre-build copy rather than an ``on_files`` File injection: ``hooks:`` run *after*
plugins for a given event, so files appended in ``on_files`` arrive too late for
mkdocs-static-i18n and trip its "Unhandled file case" warning. Seeding them on disk
before discovery avoids that entirely.

``rule-packs/`` stays at the repo root: it is a real artifact dir consumed by da-tools
and the generators (generate_platform_data / generate_rule_pack_readme / …). Only its
markdown docs are surfaced as site pages (nav: ``rule-packs/README.md`` +
``rule-packs/ALERT-REFERENCE.md``); the rule-pack YAMLs stay data, not pages. The
transient ``docs/rule-packs/`` copy is gitignored and removed in ``on_post_build``.

This hook runs for every mkdocs invocation (local wrapper, docs-ci, and the raw
``mkdocs gh-deploy`` in mkdocs-deploy.yaml), so one mechanism covers all paths.
"""

import shutil
from pathlib import Path

# Markdown docs surfaced into the site. ALERT-REFERENCE.en.md is the i18n EN pair
# of ALERT-REFERENCE.md (docs_structure: suffix); README.md is ZH-only today.
_RULE_PACK_DOCS = ("README.md", "ALERT-REFERENCE.md", "ALERT-REFERENCE.en.md")


def _dest_dir(config):
    repo_root = Path(config["config_file_path"]).parent
    return repo_root / "docs" / "rule-packs", repo_root / "rule-packs"


def on_pre_build(config):
    """Seed docs/rule-packs/ from the repo-root rule-packs/ before file discovery."""
    dest, src = _dest_dir(config)
    dest.mkdir(parents=True, exist_ok=True)
    for name in _RULE_PACK_DOCS:
        s = src / name
        if s.is_file():
            shutil.copy2(s, dest / name)


def on_post_build(config):
    """Remove the transient copy so the working tree stays clean."""
    dest, _ = _dest_dir(config)
    shutil.rmtree(dest, ignore_errors=True)
