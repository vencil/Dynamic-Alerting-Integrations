"""`make configmap-assemble` 的前置檢查：只擋加寬所武裝的那一格（#1603）。

`configmap-assemble` 是客戶的部署步驟（`docs/integration/gitops-deployment.md`
教 `make configmap-assemble` → `kubectl apply`）。#1603 把它的選檔從 `*.yaml`
放寬成 exporter 自己那兩種拼法，同時武裝了一個更糟的失效：同一個 stem 的兩種
拼法（`db-c.yaml` ＋ `db-c.yml`）放寬後都會進 ConfigMap ⇒ `config_hierarchy.go`
判跨檔重複租戶、`config.go` 的 `runHierarchyScanReject` 對它 hard reject、不
commit 任何狀態 ⇒ 整棵樹每一個租戶都失去告警，不只那一個。

⛔ 三個刻意的邊界：

1. **不重寫規則**——`validate_config` 的 `tenant_uniqueness`（#1577）已經在
   回答這個問題，這支只消費它的裁決。量測與三態分類住在
   `_lib_tenant_uniqueness`，與 `assemble_config_dir`（另一個 producer，
   #1794）共用；**措辭不共用**，因為兩者拒絕的理由不同。
2. **不採用它的完整裁決**——那會讓客戶樹只要有任何一項無關違規就從「能部署」
   變成「不能部署」，是與本軸無關的迴歸。
3. **「量不到」不是「量了沒事」**——JSON 解不出來、那一項不在輸出裡、或它回的
   **不是**肯定的 `pass`（`tenant_uniqueness` 的 `warn` 逐字說「this is a limit
   on what was checked, not a clean result」），一律 `EXIT_CALLER_ERROR`。

⛔ 裁決的對象是**產物、不是那棵樹**：ConfigMap 的 key 平面表達不出子目錄，所以
只有頂層那組檔進得去。拿整棵樹去問，會用一個永遠不會出貨的重複宣告（例如
`examples/` 底下那份）擋掉一個其實可部署的產物。子目錄裡的租戶不會出貨這件事由
`warn_nested` 說出來——那是同一份誠實的另一半。

大小寫殘留只回報不擋：`DB-A.YAML` 這種載體 exporter 收、這一步不收。glob 比對是
**shell** 做的、大小寫敏感，所以那是固定的丟棄，不隨檔案系統而變（已實測）。這個
軸是 #1792（#1588 收的是 Python reader 那一半、已 CLOSED），在它落地前那則 WARN
是唯一的訊號。
"""
from __future__ import annotations

import argparse
import os
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
from _lib_confd import has_yaml_extension, is_hidden_name, warn_nested  # noqa: E402
import _lib_tenant_uniqueness as tu  # noqa: E402

# The set the Makefile recipe ships: POSIX `*.yaml` + `*.yml`, i.e. lowercase
# only. Kept here as the ONE place that describes what that recipe does, so
# the residue report below cannot drift away from it silently.
_SHIPPED_SUFFIXES = (".yaml", ".yml")


def _shipped(config_dir: Path) -> list[Path]:
    """Exactly what `for f in <dir>/*.yaml <dir>/*.yml` yields.

    ⛔ `is_hidden_name` is not decoration: a POSIX glob never matches a
    leading dot, so `.gitlab-ci.yml` is NOT shipped. Counting it as a
    carrier made the empty-dir guard below pass for a `--config-dir`
    pointed at a repo root — the exact mis-pointing that guard exists for.
    """
    return sorted(
        p for p in config_dir.iterdir()
        if p.is_file() and not is_hidden_name(p.name)
        and p.name.endswith(_SHIPPED_SUFFIXES)
    )


def _exporter_accepts(config_dir: Path) -> list[Path]:
    """What the exporter's own rule accepts at this level (case-insensitive).

    ⛔ The exporter skips `.`-prefixed entries too (`config_hierarchy.go`),
    so leaving them in here made the residue report claim the exporter reads
    an editor backup like `.db-a.YAML.bak.YAML`.
    """
    return sorted(
        p for p in config_dir.iterdir()
        if p.is_file() and not is_hidden_name(p.name)
        and has_yaml_extension(p.name)
    )


def _forward(verdict: tu.Verdict) -> None:
    """Print the check's own details and hint. It knows more than we do."""
    for line in verdict.details:
        print(f"  {line}", file=sys.stderr)
    if verdict.action:
        print(f"  -> {verdict.action}", file=sys.stderr)


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

    # This step is FLAT — a ConfigMap key plane cannot express a subdirectory.
    # `_lib_confd`'s contract is that a flat reader says so out loud.
    warn_nested(config_dir, tool="configmap_assemble_precheck")

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

    # Case residue: loud, not blocking. See this module's docstring.
    dropped = [p.name for p in _exporter_accepts(config_dir)
               if p not in shipped]
    if dropped:
        print(
            "WARN: the exporter's scanner accepts these and the recipe's glob "
            "does not, so they will be MISSING from the ConfigMap. Globbing is "
            "done by the shell with case-sensitive matching, so this holds "
            "even on a case-insensitive filesystem (measured). The case axis "
            "on this glob is #1792; until that lands, this line is the only "
            "signal you get: " + ", ".join(sorted(dropped)),
            file=sys.stderr,
        )

    # ⛔ Ask the question about the ARTIFACT, not about the tree. The ConfigMap
    # key plane is flat, so only `shipped` can ever reach the exporter through
    # this path; validating the whole tree blocked deployable artifacts (a
    # second declaration under `examples/`, which never ships, refused the
    # build and told the operator the exporter would reject everything).
    # The nested files that do NOT ship are disclosed by `warn_nested` above,
    # which is the other half of the same honesty.
    verdict = tu.verdict_for({p.name: p for p in shipped})

    if verdict.outcome == tu.DUPLICATE:
        print(f"ERROR: {tu.CHECK_NAME} failed — refusing to assemble a ConfigMap "
              f"the exporter would reject in full.", file=sys.stderr)
        _forward(verdict)
        return tu.EXIT_FOR[verdict.outcome]

    if verdict.outcome != tu.CLEAN:
        print(f"ERROR: {verdict.reason}", file=sys.stderr)
        _forward(verdict)
        print("       This is 'could not measure', NOT 'measured clean'.",
              file=sys.stderr)
        return tu.EXIT_FOR[verdict.outcome]

    print(f"✓ pre-check OK: {len(shipped)} carrier(s), no duplicate tenant")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
