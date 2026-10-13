"""When a missing `helm` is a FAILURE instead of a skip (#1300, #2955).

Helm-gated tests skip when `helm` is not on PATH, so a run without it reads as
green while every render assertion did nothing. Two places are supposed to
have helm, and there the skip must turn into a failure:

* CI, whose helm-installing jobs set ``VIBE_REQUIRE_HELM=1``.
* A Claude Code remote session (``CLAUDE_CODE_REMOTE=true``), where
  ``.claude/hooks/session-start.sh`` installs the pinned helm. Keyed on the
  variable the harness sets, not on one the hook would have to export: in a web
  session that attached this repo later the hook never runs at all (#1719), and
  that is exactly when the skip must not pass for green.

Each guard asks ``helm_required()`` and asserts helm is there when it returns a
reason.
"""
from __future__ import annotations

import os
import shutil

HAS_HELM = shutil.which("helm") is not None


def helm_required() -> str | None:
    """Why this run must have helm, or None when a skip is acceptable."""
    if os.environ.get("VIBE_REQUIRE_HELM") == "1":
        return "VIBE_REQUIRE_HELM=1 (the CI job installs helm)"
    if os.environ.get("CLAUDE_CODE_REMOTE") == "true":
        return ("CLAUDE_CODE_REMOTE=true (.claude/hooks/session-start.sh installs "
                "helm; check /tmp/vibe-session-start-hook.ran and rerun it as CLAUDE.md says)")
    return None
