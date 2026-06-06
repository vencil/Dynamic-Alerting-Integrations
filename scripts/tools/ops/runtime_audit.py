#!/usr/bin/env python3
"""runtime_audit.py — Git rule-packs ↔ Prometheus runtime reconciliation (#747).

The genuinely-missing reconciliation leg: a READ-ONLY, on-demand hard
comparison between the rules DECLARED in Git (``rule-packs/rule-pack-*.yaml``)
and the rules Prometheus has actually LOADED (``GET /api/v1/rules``).

Why this exists (and why it is NOT a controller)
-------------------------------------------------
#747 set out to replace fragile *metric-observed* orphan detection (the
``version_orphaned`` sentinel time-series) with active reconciliation. The
design converged (see docs/custom-rule-governance.md §5 "Reconciliation
boundary") (docs/custom-rule-governance.md §7.1 "Runtime 對帳邊界") on the repo's
established silent-failure idiom — same as #631 (phantom reload), #643
(silent parse fail), #652 (cardinality truncation):

    detect → report (exit code / metric) → a HUMAN decides.

Explicitly REJECTED: self-healing / a standing reconciliation Operator. A
controller with cluster write authority (a) reintroduces the rejected
machine-writes-human-plane antipattern and (b) becomes a 4th stateful SoT
that itself drifts/OOMs — the observer-paradox recursed one level up.

So this tool is a read-only diagnostic *button*, not a daemon:
  - incident-response: ``da-tools runtime-audit --prometheus http://localhost:9090``
    (via ``kubectl port-forward`` / ``exec`` — zero new infra)
  - CI / scheduled gate: add ``--ci`` (exit 1 on drift)
  - offline / unit-test: ``--runtime-json fixture.json`` (no live cluster)

What it catches (that existing signals are blind to)
----------------------------------------------------
``/api/v1/rules`` reports per-rule ``health`` + ``lastError``, so the audit is
a hard load-state comparison, not a soft "does a series exist" inference:

  MISSING    declared in Git, absent in runtime  → reload fail / projected-
             volume lag / hand-deleted configmap. (gates in --ci)
  UNHEALTHY  loaded but health == "err"           → rule evaluates with an
             error (lastError); series-observation cannot tell this apart
             from "metric legitimately absent". (gates in --ci) NOTE: a
             transient "unknown" health — a rule that loaded but has not
             evaluated yet (e.g. just after a reload) — is deliberately NOT
             flagged, to avoid false-positives on a freshly-reloaded server.
  ORPHAN     loaded but no longer declared in Git → stale rule survives in
             the cluster (the #747 core case). Scoped to DECLARED groups so
             unrelated infra rules are not flagged. (warn; --strict-orphan
             promotes to a --ci gate)

Scope (MVP — kept deliberately narrow)
--------------------------------------
- Two-way: Git rule-pack files ↔ Prometheus runtime. The intermediate
  ConfigMap/projected-volume leg is phase-2 (Git↔ConfigMap PR-time drift is
  already a hard CI gate via #711/#714).
- Presence + health only. ``expr`` drift is phase-2: Prometheus reformats
  expressions on load, so a naive string compare false-positives; doing it
  right needs PromQL-aware normalization.

Known limitations (honest boundaries — not claimed away)
--------------------------------------------------------
- ``declared`` = every ``rule-pack-*.yaml`` in ``--rule-packs-dir``. Platform
  packs can be SELECTIVELY enabled (projected-volume ``optional``); a
  deployment that loads only a subset will see the disabled packs reported as
  MISSING. Lever: point ``--rule-packs-dir`` at only the enabled packs. (The
  ORPHAN direction is unaffected — it is scoped to declared groups.)
- Rule identity is ``(group, name)``. A pathological same-group collision
  (a ``record:`` and an ``alert:`` sharing one name in one group) is not
  distinguished; conventional naming (``level:metric:op`` vs ``CamelCase``)
  makes this effectively impossible in practice, so it is documented, not
  guarded.
- Single Prometheus endpoint per run. A sharded / federated topology
  (rule-pack-split edge vs central) requires pointing ``--prometheus`` at the
  instance that actually loads the packs in ``--rule-packs-dir``.

Usage:
    da-tools runtime-audit --prometheus http://localhost:9090
    da-tools runtime-audit --prometheus URL --ci          # CI/scheduled gate
    da-tools runtime-audit --prometheus URL --strict-orphan
    da-tools runtime-audit --runtime-json rules.json      # offline (fixture)
    da-tools runtime-audit --prometheus URL --json        # machine-readable

Exit codes (canonical, see scripts/tools/_lib_exitcodes.py):
    0  clean (or findings present but not in --ci mode)
    1  drift found AND --ci (MISSING/UNHEALTHY always; ORPHAN if --strict-orphan)
    2  caller error — cannot reach Prometheus / no runtime source / bad input
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_python import detect_cli_lang, http_get_json  # noqa: E402
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK,
    EXIT_VIOLATION,
    EXIT_CALLER_ERROR,
)

# Finding categories.
MISSING = "MISSING"
UNHEALTHY = "UNHEALTHY"
ORPHAN = "ORPHAN"

# i18n strings.
STRINGS = {
    "en": {
        "title": "Dynamic Alerting — Runtime Rule Audit (#747)",
        "result": "Result",
        "clean": "in sync — every declared rule loaded and healthy",
        "missing_d": "declared in Git but NOT loaded in Prometheus",
        "unhealthy_d": "loaded but evaluating with an error",
        "orphan_d": "loaded but no longer declared in Git (stale)",
        "cat_missing": "MISSING", "cat_unhealthy": "UNHEALTHY", "cat_orphan": "ORPHAN",
        "summary": "{m} missing, {u} unhealthy, {o} orphan "
                   "({d} declared, {r} runtime, {g} declared groups)",
        "err_no_source": "no runtime source: pass --prometheus URL or --runtime-json FILE",
        "err_prom": "cannot reach Prometheus",
        "err_runtime_json": "cannot read --runtime-json file",
        "err_rule_packs": "rule-packs dir not found or empty",
        "err_parse": "malformed input",
    },
    "zh": {
        "title": "Dynamic Alerting — Runtime 規則對帳 (#747)",
        "result": "結果",
        "clean": "已一致 — 每條宣告規則皆已載入且健康",
        "missing_d": "Git 已宣告但 Prometheus 未載入",
        "unhealthy_d": "已載入但評估出錯",
        "orphan_d": "已載入但 Git 不再宣告（孤兒殘留）",
        "cat_missing": "缺漏", "cat_unhealthy": "不健康", "cat_orphan": "孤兒",
        "summary": "{m} 缺漏, {u} 不健康, {o} 孤兒 "
                   "（宣告 {d} 條, runtime {r} 條, 宣告群組 {g} 個）",
        "err_no_source": "未提供 runtime 來源：請給 --prometheus URL 或 --runtime-json FILE",
        "err_prom": "無法連線 Prometheus",
        "err_runtime_json": "無法讀取 --runtime-json 檔",
        "err_rule_packs": "rule-packs 目錄不存在或為空",
        "err_parse": "輸入格式錯誤",
    },
}


def i18n(key: str, lang: str = "en") -> str:
    return STRINGS.get(lang, STRINGS["en"]).get(key, key)


class Finding:
    """A single reconciliation finding (a rule that is out of sync)."""

    def __init__(self, category: str, group: str, name: str,
                 rule_type: str = "", detail: str = ""):
        self.category = category   # MISSING | UNHEALTHY | ORPHAN
        self.group = group
        self.name = name
        self.rule_type = rule_type
        self.detail = detail

    def key(self) -> str:
        return f"{self.group}/{self.name}"

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "group": self.group,
            "name": self.name,
            "type": self.rule_type,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Pure functions — no I/O, no network. These are the unit-tested core.
# ---------------------------------------------------------------------------

def parse_declared_rules(
    rule_pack_paths: list[Path],
) -> dict[tuple[str, str], dict[str, str]]:
    """Parse declared rules from rule-pack YAML files.

    Returns a map keyed by ``(group_name, rule_name)`` → ``{"type": ...}``.
    ``rule_name`` is the ``alert:`` or ``record:`` value. Raises ValueError on
    malformed YAML so the caller can map it to a caller-error exit.
    """
    declared: dict[tuple[str, str], dict[str, str]] = {}
    for path in rule_pack_paths:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError) as exc:
            raise ValueError(f"{path}: {exc}") from exc
        if not isinstance(doc, dict):
            continue
        # Fail loud on a structurally-wrong top-level `groups:` (e.g.
        # `groups: "foo"` or `groups: 123`) rather than iterating a string
        # into a cryptic AttributeError. Mirrors rule_pack_diff.py's validation
        # and dev-rule #5 (fail-loud). --rule-packs-dir accepts any directory,
        # so this input is not fully trusted. (CodeRabbit PR #780.)
        groups = doc.get("groups")
        if groups is not None and not isinstance(groups, list):
            raise ValueError(
                f"{path}: top-level 'groups:' must be a list, "
                f"got {type(groups).__name__}")
        for group in groups or []:
            if not isinstance(group, dict):
                continue
            gname = group.get("name", "")
            for rule in group.get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                if "alert" in rule:
                    name, rtype = rule["alert"], "alerting"
                elif "record" in rule:
                    name, rtype = rule["record"], "recording"
                else:
                    continue
                declared[(gname, name)] = {"type": rtype}
    return declared


def parse_runtime_rules(
    api_json: dict[str, Any],
) -> dict[tuple[str, str], dict[str, str]]:
    """Parse loaded rules from a Prometheus ``/api/v1/rules`` JSON response.

    Returns a map keyed by ``(group_name, rule_name)`` →
    ``{"type", "health", "lastError"}``. Raises ValueError on a malformed /
    non-success response.
    """
    if not isinstance(api_json, dict) or api_json.get("status") != "success":
        raise ValueError(
            (api_json or {}).get("error", "non-success /api/v1/rules response")
            if isinstance(api_json, dict) else "non-dict /api/v1/rules response"
        )
    runtime: dict[tuple[str, str], dict[str, str]] = {}
    # Defensive: a proxy may return {"status":"success","data":null} or a
    # group/rules of an unexpected type — coerce rather than crash with an
    # uncaught AttributeError (the caller only maps ValueError to exit 2).
    for group in (api_json.get("data") or {}).get("groups") or []:
        if not isinstance(group, dict):
            continue
        gname = group.get("name", "")
        for rule in group.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            name = rule.get("name", "")
            if not name:
                continue
            runtime[(gname, name)] = {
                "type": rule.get("type", ""),
                "health": rule.get("health", "unknown"),
                "lastError": rule.get("lastError", ""),
            }
    return runtime


def diff_rules(
    declared: dict[tuple[str, str], dict[str, str]],
    runtime: dict[tuple[str, str], dict[str, str]],
) -> list[Finding]:
    """Reconcile declared vs runtime rules. Pure — the heart of the audit.

    - MISSING:   declared key absent from runtime.
    - UNHEALTHY: key in both, runtime health != "ok".
    - ORPHAN:    runtime key absent from declared, BUT only within a group
                 that IS declared — so unrelated infra rule groups loaded in
                 the same Prometheus are not falsely flagged.
    """
    findings: list[Finding] = []
    declared_groups = {g for (g, _n) in declared}

    for (g, n), meta in sorted(declared.items()):
        rt = runtime.get((g, n))
        if rt is None:
            findings.append(Finding(MISSING, g, n, meta["type"]))
        elif rt.get("health") == "err":
            # Only a CONFIRMED evaluation error gates. `health == "unknown"`
            # is transient — a rule that loaded but has not completed its
            # first evaluation yet (e.g. moments after a reload) reports
            # "unknown"; flagging it would false-positive a freshly-reloaded
            # Prometheus. Distinguishing "stuck unknown" from "transient
            # unknown" needs lastEvaluation timestamps (phase-2).
            findings.append(Finding(
                UNHEALTHY, g, n, rt.get("type", meta["type"]),
                detail=(rt.get("lastError") or "health=err"),
            ))

    for (g, n), rt in sorted(runtime.items()):
        if g in declared_groups and (g, n) not in declared:
            findings.append(Finding(ORPHAN, g, n, rt.get("type", "")))

    return findings


# ---------------------------------------------------------------------------
# Auditor — thin I/O shell around the pure core.
# ---------------------------------------------------------------------------

class RuntimeAuditor:
    def __init__(self, args):
        self.args = args
        self.lang = detect_cli_lang()
        self.findings: list[Finding] = []
        self.caller_error: Optional[str] = None
        self.declared_count = 0
        self.runtime_count = 0
        self.declared_group_count = 0

    def _load_declared(self) -> Optional[dict]:
        rule_dir = Path(self.args.rule_packs_dir)
        paths = sorted(rule_dir.glob("rule-pack-*.yaml")) if rule_dir.is_dir() else []
        if not paths:
            self.caller_error = i18n("err_rule_packs", self.lang)
            return None
        try:
            declared = parse_declared_rules(paths)
        except ValueError as exc:
            self.caller_error = f"{i18n('err_parse', self.lang)}: {str(exc)[:80]}"
            return None
        self.declared_count = len(declared)
        self.declared_group_count = len({g for (g, _n) in declared})
        return declared

    def _load_runtime(self) -> Optional[dict]:
        if self.args.runtime_json:
            try:
                raw = json.loads(Path(self.args.runtime_json).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                self.caller_error = f"{i18n('err_runtime_json', self.lang)}: {str(exc)[:60]}"
                return None
        elif self.args.prometheus:
            data, err = http_get_json(
                f"{self.args.prometheus}/api/v1/rules", timeout=15
            )
            if err:
                self.caller_error = f"{i18n('err_prom', self.lang)}: {err[:60]}"
                return None
            raw = data
        else:
            self.caller_error = i18n("err_no_source", self.lang)
            return None
        try:
            runtime = parse_runtime_rules(raw)
        except ValueError as exc:
            self.caller_error = f"{i18n('err_parse', self.lang)}: {str(exc)[:80]}"
            return None
        self.runtime_count = len(runtime)
        return runtime

    def run(self) -> None:
        declared = self._load_declared()
        if declared is None:
            return
        runtime = self._load_runtime()
        if runtime is None:
            return
        self.findings = diff_rules(declared, runtime)

    # -- reporting --

    def _counts(self) -> tuple[int, int, int]:
        m = sum(1 for f in self.findings if f.category == MISSING)
        u = sum(1 for f in self.findings if f.category == UNHEALTHY)
        o = sum(1 for f in self.findings if f.category == ORPHAN)
        return m, u, o

    def print_human_report(self) -> None:
        print(f"\n=== {i18n('title', self.lang)} ===")
        if self.caller_error:
            print(f"  ✗ {self.caller_error}")
            return
        if not self.findings:
            print(f"  ✓ {i18n('clean', self.lang)}")
        else:
            cat_meta = {
                MISSING: ("✗", "cat_missing", "missing_d"),
                UNHEALTHY: ("✗", "cat_unhealthy", "unhealthy_d"),
                ORPHAN: ("⚠", "cat_orphan", "orphan_d"),
            }
            for cat in (MISSING, UNHEALTHY, ORPHAN):
                items = [f for f in self.findings if f.category == cat]
                if not items:
                    continue
                glyph, label_k, desc_k = cat_meta[cat]
                print(f"\n  {glyph} {i18n(label_k, self.lang)} "
                      f"({i18n(desc_k, self.lang)}):")
                for f in items:
                    suffix = f" — {f.detail}" if f.detail else ""
                    print(f"      {f.key()} [{f.rule_type}]{suffix}")
        m, u, o = self._counts()
        print(f"\n  {i18n('result', self.lang)}: " + i18n("summary", self.lang).format(
            m=m, u=u, o=o, d=self.declared_count, r=self.runtime_count,
            g=self.declared_group_count,
        ))

    def print_json_report(self) -> None:
        if self.caller_error:
            print(json.dumps({"error": self.caller_error}, ensure_ascii=False, indent=2))
            return
        m, u, o = self._counts()
        report = {
            "findings": [f.to_dict() for f in self.findings],
            "summary": {
                "missing": m, "unhealthy": u, "orphan": o,
                "declared": self.declared_count,
                "runtime": self.runtime_count,
                "declared_groups": self.declared_group_count,
            },
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))

    def exit_code(self) -> int:
        if self.caller_error:
            return EXIT_CALLER_ERROR
        if not self.args.ci:
            return EXIT_OK
        m, u, o = self._counts()
        gating = m + u + (o if self.args.strict_orphan else 0)
        return EXIT_VIOLATION if gating > 0 else EXIT_OK


def main():
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Read-only Git rule-packs ↔ Prometheus runtime reconciliation (#747)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--rule-packs-dir", default="rule-packs/",
        help="Directory with rule pack YAML files (default: rule-packs/)",
    )
    src = parser.add_argument_group("runtime source (one required)")
    src.add_argument(
        "--prometheus",
        help="Prometheus API URL, e.g. http://localhost:9090",
    )
    src.add_argument(
        "--runtime-json",
        help="Path to a saved /api/v1/rules JSON response (offline / fixtures)",
    )
    parser.add_argument(
        "--strict-orphan", action="store_true",
        help="Treat ORPHAN (stale loaded rule) as a --ci gate failure too",
    )
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument(
        "--ci", action="store_true",
        help="Exit 1 if drift is found (MISSING/UNHEALTHY; ORPHAN with --strict-orphan)",
    )

    args = parser.parse_args()

    auditor = RuntimeAuditor(args)
    auditor.run()

    if args.json:
        auditor.print_json_report()
    else:
        auditor.print_human_report()

    sys.exit(auditor.exit_code())


if __name__ == "__main__":
    main()
