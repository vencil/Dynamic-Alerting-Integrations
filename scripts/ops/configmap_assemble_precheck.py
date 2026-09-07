"""`make configmap-assemble` 的前置檢查：只擋加寬所武裝的那一格（#1603）。

`configmap-assemble` 是客戶的部署步驟（`docs/integration/gitops-deployment.md`
教 `make configmap-assemble` → `kubectl apply`）。#1603 把它的選檔從 `*.yaml`
放寬成 exporter 自己那兩種拼法，同時武裝了一個更糟的失效：同一個 stem 的兩種
拼法（`db-c.yaml` ＋ `db-c.yml`）放寬後都會進 ConfigMap ⇒ `config_hierarchy.go`
判跨檔重複租戶、`config.go` 的 `runHierarchyScanReject` 對它 hard reject、不
commit 任何狀態 ⇒ 整棵樹每一個租戶都失去告警，不只那一個。

⛔ 三個刻意的邊界：

1. **不重寫規則**——`validate_config` 的 `tenant_uniqueness`（#1577）已經在
   回答這個問題，這支只消費它的 `--json` 逐項輸出。
2. **不採用它的完整裁決**——那會讓客戶樹只要有任何一項無關違規就從「能部署」
   變成「不能部署」，是與本軸無關的迴歸。
3. **「量不到」不是「量了沒事」**——JSON 解不出來、或那一項不在輸出裡，一律
   `EXIT_CALLER_ERROR`。

大小寫殘留只回報不擋（#1588 的軸）：shell 沒有可攜的大小寫不敏感 glob，所以
`DB-A.YAML` 這種載體 exporter 收、這一步不收；差集逐檔印出來。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_TOOLS = _THIS_DIR.parent / "tools"
sys.path.insert(0, str(_TOOLS))
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK,
    EXIT_VIOLATION,
    EXIT_CALLER_ERROR,
)
from _lib_confd import has_yaml_extension  # noqa: E402

_VALIDATE = _TOOLS / "ops" / "validate_config.py"
CHECK_NAME = "tenant_uniqueness"
# Generous: this walks the customer's whole conf.d tree. It is a hang guard,
# not a performance budget.
_VALIDATE_TIMEOUT_S = 300

# The set the Makefile recipe ships: POSIX `*.yaml` + `*.yml`, i.e. lowercase
# only. Kept here as the ONE place that describes what that recipe does, so
# the residue report below cannot drift away from it silently.
_SHIPPED_SUFFIXES = (".yaml", ".yml")


def _shipped(config_dir: Path) -> list[Path]:
    """Exactly what `for f in <dir>/*.yaml <dir>/*.yml` yields."""
    return sorted(
        p for p in config_dir.iterdir()
        if p.is_file() and p.name.endswith(_SHIPPED_SUFFIXES)
    )


def _exporter_accepts(config_dir: Path) -> list[Path]:
    """What the exporter's own rule accepts at this level (case-insensitive)."""
    return sorted(
        p for p in config_dir.iterdir()
        if p.is_file() and has_yaml_extension(p.name)
    )


def _run_validate(config_dir: Path) -> list[dict]:
    """`validate_config --json`, parsed. Raises RuntimeError with a reason."""
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(_VALIDATE),
             "--config-dir", str(config_dir), "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            # A hang here would wedge `make configmap-assemble` — a target
            # customers run — with no output at all. Bounded, and the timeout
            # lands in the SAME "could not measure" arm as a parse failure.
            timeout=_VALIDATE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"validate_config did not finish within {_VALIDATE_TIMEOUT_S}s"
        ) from exc
    # ⛔ The return code is deliberately IGNORED: a non-zero rc here means
    # "some check failed", and this gate only speaks for one of them.
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError(
            f"validate_config produced no stdout (rc={proc.returncode}). "
            f"stderr: {proc.stderr.strip()[:400] or '(empty)'}"
        )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"validate_config --json was not parseable: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError(
            f"validate_config --json returned {type(data).__name__}, expected a list"
        )
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Pre-check for `make configmap-assemble` (#1603).")
    ap.add_argument("--config-dir", required=True,
                    help="conf.d directory the ConfigMap is assembled from")
    args = ap.parse_args(argv)

    config_dir = Path(args.config_dir)
    if not config_dir.is_dir():
        print(f"ERROR: config-dir not found: {config_dir}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    shipped = _shipped(config_dir)
    if not shipped:
        print(
            f"ERROR: {config_dir} contains no config carrier — the recipe's "
            f"globs would expand to nothing and the ConfigMap would be built "
            f"with zero --from-file args. That is a mis-pointed --config-dir "
            f"far more often than an intentional deploy.\n"
            f"       ⚠️ NOT a tenant-count policy: a dir holding only "
            f"`_defaults.yaml` is allowed, because the ConfigMap then "
            f"faithfully projects a platform that has no tenants yet. "
            f"Whether a tree SHOULD have tenants is validate_config's plane.",
            file=sys.stderr,
        )
        return EXIT_VIOLATION

    # #1588 residue: loud, not blocking. See this module's docstring.
    dropped = [p.name for p in _exporter_accepts(config_dir)
               if p not in shipped]
    if dropped:
        print(
            "WARN: the exporter's scanner accepts these; whether the recipe's "
            "POSIX glob does depends on the filesystem (case-sensitive: no, "
            "macOS default: yes), so this is a portability split, not a fixed "
            "drop. Extension-case is #1588's axis: " + ", ".join(sorted(dropped)),
            file=sys.stderr,
        )

    try:
        checks = _run_validate(config_dir)
    except RuntimeError as exc:
        print(f"ERROR: could not run the duplicate-tenant check — {exc}",
              file=sys.stderr)
        print("       This is 'could not measure', NOT 'measured clean'.",
              file=sys.stderr)
        return EXIT_CALLER_ERROR

    entry = next((c for c in checks
                  if isinstance(c, dict) and c.get("check") == CHECK_NAME), None)
    if entry is None:
        names = sorted(str(c.get("check")) for c in checks if isinstance(c, dict))
        print(
            f"ERROR: validate_config did not report a `{CHECK_NAME}` check; "
            f"it reported: {names}. This gate cannot confirm the config dir "
            f"is free of cross-file duplicate tenants, so it refuses rather "
            f"than assuming it is.",
            file=sys.stderr,
        )
        return EXIT_CALLER_ERROR

    if entry.get("status") == "fail":
        print(f"ERROR: {CHECK_NAME} failed — refusing to assemble a ConfigMap "
              f"the exporter would reject in full.", file=sys.stderr)
        for line in entry.get("details") or []:
            print(f"  {line}", file=sys.stderr)
        action = entry.get("suggested_action")
        if action:
            print(f"  -> {action}", file=sys.stderr)
        return EXIT_VIOLATION

    print(f"✓ pre-check OK: {len(shipped)} carrier(s), no duplicate tenant")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
