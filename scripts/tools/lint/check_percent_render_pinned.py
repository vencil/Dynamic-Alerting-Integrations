#!/usr/bin/env python3
"""check_percent_render_pinned — a percent-rendered annotation must be pinned by a firing test.

Problem (#978)
--------------
Rule packs render percentages two ways: a percent-scale value with
``{{ $value | printf "%.1f" }}%`` and a 0-1 ratio with ``{{ $value | humanizePercentage }}``.
Mixing them inside one alert is silent: a ratio fed to the printf form shows 100% as
"1%" (#975 fixed exactly that), a percent fed to humanizePercentage shows 85 as "8500%".
Inferring the scale from the expression is not reliable (``:ratio`` record names lie,
raw metrics are already percent), so this does not try.

What this guards
----------------
Every annotation whose template renders a percentage (``humanizePercentage`` or a
``}}%`` suffix) must be asserted, under the same annotation key, in a FIRING case: an
``alert_rule_test`` with a non-empty ``exp_alerts`` whose ``exp_annotations`` has that
key, in some tests/rulepacks/*_test.{yaml,yml}. The rendered text is then written out
literally in the fixture, so "8500%" or "1%" is in the diff for the author and the
reviewer to see.

Alerts that broke the rule when the gate arrived are listed in
tests/rulepacks/percent_render_baseline.yaml, bidirectionally like
vmalert_coverage_baseline.yaml:
  * an unpinned (alert, annotation) not in the baseline  -> FAIL
  * a baseline entry that is now pinned / gone            -> FAIL (remove it)

Usage:
  check_percent_render_pinned.py            # check (0 ok / 1 finding / 2 unreadable input)
  check_percent_render_pinned.py --ci       # same, for CI
  check_percent_render_pinned.py --generate # rewrite the baseline from the tree
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _lib_compat  # noqa: E402,F401  (import-time side effect; see _lib_compat)
import _lib_consumer_read  # noqa: E402  (rule packs as Prometheus reads them)
from _lib_validation import i18n_text  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    print("check_percent_render_pinned: PyYAML required (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[3]
_RULE_PACKS = _REPO / "rule-packs"
_TESTS = _REPO / "tests" / "rulepacks"
_BASELINE = _TESTS / "percent_render_baseline.yaml"

# A percentage is rendered when the template pipes into humanizePercentage, when an
# action is closed and immediately followed by a percent sign ("}}%", "}} %", or the
# full-width "％" a Chinese annotation may use), or when a printf format inside an
# action writes one itself ("%.1f%%").
PERCENT_RENDER = re.compile(r"humanizePercentage|\}\}\s*[%％]|\{\{[^}]*%%")

_BASELINE_HEADER = """\
# Percent-rendered annotations with no firing test that asserts them (#978).
# Managed by scripts/tools/lint/check_percent_render_pinned.py (bidirectional):
#   * an unpinned (alert, annotation) not listed here -> CI FAIL
#   * a listed entry that is now pinned / removed     -> CI FAIL (remove it)
# "Pinned" = some alert_rule_test for the alert has a non-empty exp_alerts whose
# exp_annotations carries that annotation key. Listing here is tracked debt, not approval.
# Regenerate after an intentional change:
#   python3 scripts/tools/lint/check_percent_render_pinned.py --generate
"""


class RulePacksRefused(Exception):
    """A rule pack Prometheus' rule loader refuses (each named)."""


def _load_yaml(p: Path) -> dict:
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{p}: the top-level YAML document is not a mapping")
    return data


def _fixture_files() -> list[Path]:
    return sorted(f for f in _TESTS.iterdir()
                  if f.is_file() and (f.name.endswith("_test.yaml") or f.name.endswith("_test.yml")))


def percent_rendered() -> dict[str, dict[str, list[str]]]:
    """{pack_filename: {alert: [annotation keys that render a percentage]}}."""
    found = _lib_consumer_read.rule_files_in(
        _lib_consumer_read.rule_pack_files(_RULE_PACKS), keep_refusals=True)
    rule_files = [rf for rfs in found.values() for rf in rfs]
    refused = [f"{Path(rf.path).name}: {rf.problem}" for rf in rule_files
               if rf.problem is not None]
    if refused:
        raise RulePacksRefused("rule pack(s) Prometheus' rule loader refuses: "
                               + "; ".join(refused))
    out: dict[str, dict[str, list[str]]] = {}
    for rf in rule_files:
        pack = Path(rf.manifest).name
        for grp in rf.groups:
            for rule in (grp.get("rules") or []):
                alert = rule.get("alert")
                if not alert:
                    continue
                keys = sorted(k for k, v in (rule.get("annotations") or {}).items()
                              if isinstance(v, str) and PERCENT_RENDER.search(v))
                if keys:
                    # Two rules may share an alertname: union their keys, never
                    # let the later one hide the earlier one's.
                    have = out.setdefault(pack, {}).setdefault(alert, [])
                    out[pack][alert] = sorted(set(have) | set(keys))
    return out


def _loaded_rule_packs(fixture: Path, data: dict) -> set[str]:
    """The rule packs under rule-packs/ (names _lib_consumer_read.RULE_PACKS
    declares) that the fixture's rule_files load, by file name — or none when it
    also loads any other rule file. An assertion speaks for the pack only when
    the pack is all the fixture loads: next to a local *.rules.yaml holding an
    alert of the same name, the rendered text may be the local rule's (#4029)."""
    out = set()
    for rf in (data.get("rule_files") or []):
        target = (fixture.parent / str(rf)).resolve()
        if (target.parent == _RULE_PACKS.resolve()
                and _lib_consumer_read.RULE_PACKS.member(target) is not None):
            out.add(target.name)
        else:
            return set()
    return out


def pinned_annotations() -> dict[tuple[str, str], set[str]]:
    """{(pack file, alert): annotation keys asserted by a FIRING case (non-empty
    exp_alerts)} for every rule pack the fixture loads."""
    out: dict[tuple[str, str], set[str]] = {}
    for f in _fixture_files():
        data = _load_yaml(f)
        packs = _loaded_rule_packs(f, data)
        for t in (data.get("tests") or []):
            for art in (t.get("alert_rule_test") or []):
                name = art.get("alertname")
                for exp in (art.get("exp_alerts") or []):
                    for key in ((exp or {}).get("exp_annotations") or {}):
                        if name:
                            for pack in packs:
                                out.setdefault((pack, name), set()).add(key)
    return out


def current_unpinned() -> dict[str, dict[str, list[str]]]:
    pinned = pinned_annotations()
    out: dict[str, dict[str, list[str]]] = {}
    for pack, alerts in sorted(percent_rendered().items()):
        for alert, keys in sorted(alerts.items()):
            missing = [k for k in keys if k not in pinned.get((pack, alert), set())]
            if missing:
                out.setdefault(pack, {})[alert] = missing
    return out


def load_baseline() -> dict[str, dict[str, list[str]]]:
    if not _BASELINE.exists():
        return {}
    data = _load_yaml(_BASELINE).get("unpinned") or {}
    return {pack: {a: sorted(ks or []) for a, ks in (alerts or {}).items()}
            for pack, alerts in data.items()}


def _flatten(tree: dict[str, dict[str, list[str]]]) -> set[tuple[str, str, str]]:
    return {(pack, alert, key) for pack, alerts in tree.items()
            for alert, keys in alerts.items() for key in keys}


def generate() -> None:
    tree = current_unpinned()
    body = yaml.safe_dump({"unpinned": tree}, allow_unicode=True, sort_keys=True,
                          default_flow_style=False)
    _BASELINE.write_text(_BASELINE_HEADER + body, encoding="utf-8", newline="\n")
    n = len(_flatten(tree))
    print(f"check_percent_render_pinned: wrote baseline with {n} unpinned annotation(s) "
          f"-> {_BASELINE.relative_to(_REPO)}")


def check() -> int:
    now = _flatten(current_unpinned())
    base = _flatten(load_baseline())
    new = sorted(now - base)
    stale = sorted(base - now)
    if not new and not stale:
        total = len(_flatten(percent_rendered()))
        print(f"check_percent_render_pinned: OK — {total - len(now)} percent-rendered "
              f"annotation(s) pinned by a firing test, {len(now)} baselined (== reality).")
        return 0
    if new:
        print("check_percent_render_pinned: percent-rendered annotation(s) that no firing test "
              "asserts. Add an alert_rule_test with a non-empty exp_alerts whose "
              "exp_annotations spells out the rendered text, so the scale shows in the diff:")
        for pack, alert, key in new:
            print(f"    + {alert}.{key}   [{pack}]")
    if stale:
        print("check_percent_render_pinned: stale baseline entry(ies) — now pinned, or the alert / "
              f"annotation is gone. Remove them from {_BASELINE.relative_to(_REPO)} (--generate):")
        for pack, alert, key in stale:
            print(f"    - {alert}.{key}   [{pack}]")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=i18n_text(
        "以百分比渲染的 annotation（humanizePercentage、}}% 或 printf 的 %%）必須有一個 "
        "exp_alerts 非空的測試，在 exp_annotations 逐字寫出同一個 key 的渲染結果（#978）。"
        "既有缺口列在 tests/rulepacks/percent_render_baseline.yaml，雙向比對。",
        "An annotation that renders a percentage (humanizePercentage, }}% or a printf %%) "
        "must be asserted under the same key by a test with a non-empty exp_alerts (#978). "
        "Gaps at introduction are in tests/rulepacks/percent_render_baseline.yaml, "
        "compared both ways."))
    ap.add_argument("--generate", action="store_true",
                    help=i18n_text("依目前的樹重寫 baseline", "rewrite the baseline from the tree"))
    ap.add_argument("--ci", action="store_true",
                    help=i18n_text("CI 模式（有發現時 exit 1）", "CI mode (exit 1 on a finding)"))
    ap.add_argument("files", nargs="*",
                    help=i18n_text("忽略（pre-commit 傳入的變更檔）",
                                   "ignored (pre-commit passes changed files)"))
    args = ap.parse_args()
    if not _RULE_PACKS.is_dir() or not _TESTS.is_dir():
        print(f"check_percent_render_pinned: rule-packs/ or tests/rulepacks/ not found under {_REPO}",
              file=sys.stderr)
        return 2
    try:
        if args.generate:
            generate()
            return 0
        return check()
    except RulePacksRefused as e:
        print(f"check_percent_render_pinned: {e}", file=sys.stderr)
        return 2
    except _lib_consumer_read.ConsumerReadError as e:
        print(f"check_percent_render_pinned: {_lib_consumer_read.error_text(e)}", file=sys.stderr)
        return 2
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f"check_percent_render_pinned: could not read the inputs: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
