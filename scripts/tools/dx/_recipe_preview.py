#!/usr/bin/env python3
"""_recipe_preview.py — recipe would-fire preview core (#657 P2).

Given ONE ADR-024 custom-alert recipe + a scenario value, answer "would it
fire?" by going through the SAME authoritative engine the platform uses —
the exporter's judgment and rules (da-guard served-values and
compile-custom-alerts, over a one-tenant conf.d holding the recipe) +
`promtool` — never re-implementing eval or the recipe rules (the
two-eval-homes rule; see docs/design/recipe-would-fire-preview.md).

MVP scope: `threshold` (ops >, >=, <, <=, ==; a flat constant series at the
scenario value) and `absence` (the metric is simply NOT emitted → it is absent
over the window). Both are faithfully reproducible with a hand-built synthetic
series. The remaining recipes fall back to `supported: false` because a flat /
absent series can't stand in for them:
  - rate / ratio / forecast / p99_latency — time-dependent; need a populated
    lookback or slope the preview can't fake yet (deferred to a later pass);
  - `selectors_re` (regex label filters) — we can't synthesize a value
    guaranteed to match an arbitrary regex, so a preview could silently report
    a false "inactive". Refusing is honest; lying is not.

Eval mechanism (§5.2): `promtool test rules` is an ASSERT tool, so the same
synthetic input is written as two groups asserting OPPOSITE answers (`fires`:
the alert fires; `quiet`: it does not, and the pack loaded) and the verdict is
which one held, read from the JUnit report — see `_FIRES` / `_VERDICT` (#3611).
A compile error must NOT be mislabeled as firing, so we gate with the
exporter's verdict on the recipe + `promtool check rules` BEFORE the test
(§5.2 layering).
"""
import json
import math
import os
import re
import shutil
import time
import sys
import tempfile
from pathlib import Path

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
import compile_custom_alerts as cc  # noqa: E402
import _lib_tenant_values as tv  # noqa: E402  (on the path compile_custom_alerts adds)
import _lib_parse_cache  # noqa: E402
import _lib_engine as engine  # noqa: E402  (#3611: a promtool run counts only on its proof)

# `threshold` covers >, >=, <, <=, == (all value-crossing, flat series). `absence`
# is presence-based — fires where a DECLARING tenant's metric had no sample over
# the window — so a "don't emit the metric" series reproduces it exactly. The
# remaining (rate/ratio/forecast/p99_latency) stay deferred (need a populated
# lookback). Per-type gating (§7): an unsupported type returns supported:false
# WITHOUT a compile, so it is never mislabeled firing/error.
# Per-type branches (here + build_preview_test + the validation gates + reason
# rendering) are deliberately NOT a registry abstraction — revisit only when a
# 3rd recipe type is actually scheduled (2026-07 ROI review).
SUPPORTED_RECIPES_MVP = frozenset({"threshold", "absence"})

# `window` and the compiled rule's `for:` are Prometheus/Go durations — PARSE them
# (not a fixed map: the allowed `for:` values are the recipe contract's) to size
# the series and the eval_time. It can be COMPOUND ("1h30m") and
# carry sub-second units ("500ms"), per the schema grammar
# ^([0-9]+(ns|us|µs|ms|s|m|h))+$ — the compiler interpolates it RAW into
# count_over_time(metric[window]) (Prometheus parses it), so a narrower parser here
# would false-ERROR a valid, would-actually-fire recipe (adversarial review). Return
# None on a malformed / zero window so the caller fail-closes to error rather than
# guess → wrong eval_time → a real firing misread (CodeRabbit #873).
_DUR_FULL_RE = re.compile(r"^([0-9]+(?:ns|us|µs|ms|s|m|h))+$")
_DUR_TOKEN_RE = re.compile(r"(\d+)(ns|us|µs|ms|s|m|h)")
# nanoseconds per unit — INTEGER, so a pathological 400-digit window stays in
# Python's arbitrary-precision int and NEVER hits an int→float OverflowError (the
# earlier `math.ceil(secs / 60)` form crashed on a huge window — CodeRabbit). µs==us.
_DUR_UNIT_NS = {"ns": 1, "us": 1000, "µs": 1000, "ms": 1_000_000,
                "s": 1_000_000_000, "m": 60_000_000_000, "h": 3_600_000_000_000}
_NS_PER_MIN = 60_000_000_000
# the preview synthesizes a 1-sample-PER-MINUTE series, so a window beyond ~a day
# is impractical to preview — cap it. This also bounds the series length so an
# absurd window fails fast as state:error instead of OOM-ing/timing-out promtool.
_MAX_WINDOW_MIN = 1440  # 24h


def _duration_minutes(d, allow_zero=False):
    """Prometheus duration (compound + sub-second ok) → whole minutes (ceil).
    INTEGER arithmetic throughout. None on a malformed / absurdly large
    (> _MAX_WINDOW_MIN) duration, and on a zero one unless *allow_zero* (a
    `for: 0s` rule fires at once; a zero window is no window) — the preview
    can't synthesize a series that long, and a recipe with such a window still
    deploys (the preview just abstains)."""
    w = str(d or "").strip()
    if not _DUR_FULL_RE.match(w):
        return None
    ns = sum(int(n) * _DUR_UNIT_NS[u] for n, u in _DUR_TOKEN_RE.findall(w))
    if ns <= 0 and not allow_zero:
        return None
    minutes = -(-ns // _NS_PER_MIN)   # integer ceil-div: no float → no overflow
    return minutes if minutes <= _MAX_WINDOW_MIN else None


def _window_minutes(window):
    """`_duration_minutes` of an absence `window` (never zero)."""
    return _duration_minutes(window)


#: Located, or the NoVerdict naming why not: promtool is this service's
#: optional engine — without it a preview says "cannot evaluate locally"
#: rather than failing (#3611 Q1: a customer tool's value-add engine).
_PROMTOOL = engine.locate("promtool")

_TIMEOUT = 60


def _timeout_left(deadline):
    """The timeout for one da-guard / promtool run: `_TIMEOUT`, cut to the
    time left before *deadline* (an absolute `time.monotonic()` value; None =
    no caller deadline). Never 0 or negative: subprocess reads a non-positive
    timeout as already expired only on some paths, so a deadline already gone
    gives a tiny positive one and the run fails fast."""
    if deadline is None:
        return _TIMEOUT
    return min(_TIMEOUT, max(0.01, deadline - time.monotonic()))


# The one-tenant conf.d the recipe is judged in: a fixed file name, so the
# tenant id (the caller's) never becomes a path.
_TENANT_FILE = "tenant.yaml"

# ── the promtool test: one definition the builder writes and the verdict reads ──
# (#3611, owner ruling 2026-11-12). promtool reports EVERY error inside a test
# group — unreadable input series, a rule that fails to load or to evaluate —
# as the same JUnit <failure> an assertion that did not hold gets (upstream
# cmd/promtool/unittest.go: tc.Fail). So no single group's failure can be read
# as "the alert fired". Two groups assert OPPOSITE answers on one input: a real
# evaluation fails exactly one of them; anything that kept promtool from
# evaluating fails both. The quiet group also asserts a recording rule this
# service adds to the pack it writes, so "inactive" carries proof the pack
# loaded (a rule_files entry promtool cannot find loads no rules, silently).

_PACK_FILE = "rule-pack-custom-alerts.yaml"
_TEST_FILE = "preview_test.yaml"
_LOADED_RECORD = "da_preview_loaded"
_LOADED_GROUP = {"name": "da-preview-loaded",
                 "rules": [{"record": _LOADED_RECORD, "expr": "vector(1)"}]}
_FIRES, _QUIET = "fires", "quiet"

#: (fires passed, quiet passed) → state. Every other combination — both
#: failed, both passed, a group missing — is no verdict (state: error).
_VERDICT = {(True, False): "firing", (False, True): "inactive"}


def _labels(d):
    """Render a Prometheus label set `{k="v", ...}` (insertion order).

    Values are written as JSON strings, which PromQL's double-quoted string
    grammar (Go's) reads back as the same value, so a selector value
    containing a quote, backslash, newline or any other character yields the
    SAME label value the compiled rule matches against — otherwise the
    synthetic series wouldn't match (false "inactive") or would be an
    unparseable series.
    """
    return "{" + ", ".join(
        f"{k}={json.dumps(str(v), ensure_ascii=False)}" for k, v in d.items()
    ) + "}"


class Judged:
    """The exporter's verdict on the recipe, read from the one-tenant tree:
    the pack, the alert name, the `user_threshold` row it serves and the
    entry as it decoded it."""

    def __init__(self, pack, row, for_min, spec):
        self.pack = pack
        self.row = row            # served-values' `_custom_alerts` row: labels, metric, severity, value
        self.for_min = for_min    # the compiled alert's `for:`, in minutes
        self.spec = spec          # the entry as the exporter decoded it (string selector values)

    @property
    def slug(self):
        return self.row["labels"]["recipe_id"]

    @property
    def alertname(self):
        return f"Custom_{self.slug}"


def _finite_or_inf(v):
    """*v* as a float, or inf when it does not fit one (an int past 1e308)."""
    try:
        return float(v)
    except OverflowError:
        return math.inf


def _fmt_value(v):
    """A served threshold (a finite float) as promtool's series grammar and a
    reader write it: an integral value without its `.0`, any other exactly."""
    v = float(v)
    return str(int(v)) if v.is_integer() and abs(v) < 2 ** 53 else repr(v)


def judge(recipe, tenant, confd, deadline=None):
    """Write *recipe* as *tenant*'s only `_custom_alerts` entry into the empty
    directory *confd*, and read the exporter's verdict on it. Returns a
    `Judged`, or a reason string when the exporter does not serve it.
    Raises what `tv.load_served_tree` / `tv.load_custom_alert_pack` raise
    (each da-guard run bounded by the time left before *deadline*,
    recomputed when it starts), and OSError."""
    text = yaml.safe_dump({"tenants": {tenant: {"_custom_alerts": [recipe]}}},
                          sort_keys=False, allow_unicode=True)
    (Path(confd) / _TENANT_FILE).write_text(text, encoding="utf-8", newline="\n")
    served = tv.load_served_tree(confd, timeout=_timeout_left(deadline)).tenants.get(tenant)
    if served is None:
        return f"the exporter serves no tenant {tenant!r} from the recipe"
    if served.custom_alerts is None:
        raise tv.ServedValuesError(
            f"da-guard {tv.SUBCOMMAND} has no custom_alerts — this da-guard is older than this "
            "tool: upgrade or rebuild it", None, "", stale=True)
    if served.custom_alerts_error:
        return f"the exporter does not decode _custom_alerts: {served.custom_alerts_error}"
    entry = served.custom_alerts[0]
    if entry.status == "rejected":
        return f"the exporter rejects the recipe: {entry.reason}"
    if entry.status == "disabled":
        return "the recipe is switched off (threshold: disable): it never fires"
    rows = served.values.get("_custom_alerts") or []
    if entry.status != "served" or len(rows) != 1:
        return f"the exporter serves {len(rows)} row(s) for the recipe ({entry.status})"
    row = rows[0]
    pack = cc.build_pack(confd, timeout=_timeout_left(deadline))
    alertname = f"Custom_{row['labels']['recipe_id']}"
    rule = next((r for g in pack.groups for r in g.get("rules", [])
                 if r.get("alert") == alertname
                 and (r.get("labels") or {}).get("severity") == row["severity"]), None)
    if rule is None:
        return f"the compiled pack has no {alertname} rule for severity {row['severity']}"
    for_min = _duration_minutes(rule.get("for", "0s"), allow_zero=True)
    if for_min is None:
        return f"preview cannot size the series for for-window {rule.get('for')!r}"
    return Judged(pack, row, for_min, entry.spec)


def build_preview_test(recipe, tenant, value, judged):
    """Build a promtool test (YAML str) for a supported recipe — two opposite groups.

    The `user_threshold` series is the row the exporter serves for the recipe
    (its labels, severity and value, `judged.row`), not one assembled here.
    *value* is a finite int / float (the caller checks it) and is written in
    the threshold's own canonical form (`_fmt_value`), never as given.

    The test is the two opposite groups described at `_FIRES` / `_QUIET`.

    POLYMORPHIC by recipe type (a flat series can't fake every shape):
      - threshold (>, >=, <, <=, ==): the observed metric @ the scenario `value`
        (+ selector labels) + `user_threshold` + a constant
        `tenant_metadata_info`; flat, held past `for:`.
      - absence: emit ONLY the declaration (`user_threshold`, which the compiler
        records as `custom:threshold:{id}`) + `tenant_metadata_info`, and DO NOT
        emit the metric. The rule is `custom:threshold:{id} unless on(tenant)
        count by(tenant)(count_over_time(metric[window]) > 0)`, so an absent
        metric leaves the `unless` arm empty → the declaring tenant fires (the
        absence.yaml firing case). `value` is unused. eval past `window` + `for:`.
    Returns the YAML text.
    """
    row = judged.row
    for_min = judged.for_min
    ut_labels = {"component": "custom", "metric": row["metric"], "tenant": tenant,
                 "severity": row["severity"], **row["labels"]}
    thr_value = _fmt_value(row["value"])

    def _md(n):
        # constant tenant_metadata_info for the alert's metadata annotations to
        # query (both shapes; the rule does not join it, #3483).
        # Routed through _labels (not hand-written) so its escaping matches the
        # metric / user_threshold series exactly — one escaping path, no drift.
        md_labels = {
            "tenant": tenant,
            "runbook_url": f"http://rb/{tenant}",
            "owner": f"team-{tenant}",
            "tier": "gold",
        }
        return {"series": f"tenant_metadata_info{_labels(md_labels)}", "values": f"1x{n}"}

    if recipe.get("recipe") == "absence":
        # eval must clear BOTH the absence detection window AND `for:`. The metric
        # is INTENTIONALLY absent (no series), so count_over_time(metric[window])
        # is empty → the rule's `unless on(tenant)` keeps the declaring tenant →
        # fires. `value` is irrelevant to a presence check.
        win_min = _window_minutes(recipe["window"])   # caller pre-validates (not None)
        eval_min = win_min + for_min + 5
        n = eval_min + 5
        input_series = [
            {"series": f"user_threshold{_labels(ut_labels)}", "values": f"{thr_value}x{n}"},
            _md(n),
        ]
    else:
        # threshold / == : a flat constant series at the scenario value, held long
        # enough to clear `for:`.
        eval_min = for_min + 5
        n = for_min + 10
        # The selector the compiled rule matches is the exporter's reading of
        # the recipe (`flag: true` is "true" to it, not Python's "True").
        selectors = dict(judged.spec.get("selectors") or {})
        metric_labels = {"tenant": tenant, **selectors}
        input_series = [
            {"series": f"{row['metric']}{_labels(metric_labels)}",
             "values": f"{_fmt_value(value)}x{n}"},
            {"series": f"user_threshold{_labels(ut_labels)}", "values": f"{thr_value}x{n}"},
            _md(n),
        ]

    # Build the promtool test as a data structure and let yaml.safe_dump handle the
    # OUTER YAML quoting — NOT a hand-assembled string. The series CONTENT is still
    # produced by _labels so each `{k="v"}` matches the compiled rule's label
    # literal; the dumper owns the scalar quoting around it. Hand-wrapping the
    # series in a single-quoted YAML scalar (the old form) silently broke on a valid
    # selector value that contains a single quote (e.g. `it's`): the `'` closed the
    # scalar early → promtool parse error → a would-actually-deploy recipe was
    # mislabeled state:error. safe_dump escapes per YAML rules, so any exact-selector
    # value round-trips to the same string promtool sees. (sort_keys=False keeps
    # promtool's expected key order.)
    firing = (f"(count(ALERTS{_labels({'alertname': judged.alertname, 'alertstate': 'firing'})})"
              f" > bool 0) or vector(0)")

    def group(name, fires, extra=()):
        return {"name": name, "interval": "1m", "input_series": input_series,
                "promql_expr_test": [
                    {"expr": firing, "eval_time": f"{eval_min}m",
                     "exp_samples": [{"labels": "{}", "value": 1 if fires else 0}]},
                    *extra]}

    # `{tenant=""}`: the service's record has no labels, every synthetic
    # series carries the tenant — a recipe whose metric is named like the
    # record is not counted with it.
    loaded = {"expr": f'count({_LOADED_RECORD}{{tenant=""}})', "eval_time": f"{eval_min}m",
              "exp_samples": [{"labels": "{}", "value": 1}]}
    doc_obj = {
        "rule_files": [_PACK_FILE],
        "evaluation_interval": "1m",
        "tests": [group(_FIRES, True), group(_QUIET, False, [loaded])],
    }
    return yaml.safe_dump(doc_obj, sort_keys=False, allow_unicode=True)


def _err(reason, alertname=None):
    return {"alertname": alertname, "supported": True,
            "states": [{"state": "error", "reason": reason}], "warnings": []}


def preview_recipe(recipe, tenant, scenario, deadline=None):
    """Would-fire preview for ONE recipe. *deadline* (an absolute
    `time.monotonic()` value, or None) bounds every da-guard / promtool run.
    Returns the §4 contract dict:
    {alertname, supported, states:[{severity, mode, state, reason}], warnings}.

    state ∈ firing | inactive | error. Per-type gating: unsupported recipe
    types return supported:false (no compile). promtool absent → supported:true
    with a warning and no states (cannot evaluate locally).
    """
    if not isinstance(recipe, dict):
        return _err(f"recipe must be a mapping, got {type(recipe).__name__}")
    rtype = recipe.get("recipe")
    if rtype not in SUPPORTED_RECIPES_MVP:
        return {
            "alertname": None,
            "supported": False,
            "states": [],
            "warnings": [
                f"would-fire preview for recipe type {rtype!r} is coming soon "
                f"(P3); supported now: {sorted(SUPPORTED_RECIPES_MVP)}"
            ],
        }

    # selectors_re (regex label filters) gate: we hand-build a flat synthetic
    # series and can't synthesize a value guaranteed to match an arbitrary
    # regex, so the compiled `{k=~"re"}` filter could exclude our series and
    # the preview would silently report a false "inactive". Refuse honestly
    # (exact `selectors` ARE reproducible — see _labels / build_preview_test).
    if recipe.get("selectors_re"):
        return {
            "alertname": None,
            "supported": False,
            "states": [],
            "warnings": [
                "would-fire preview does not yet support regex selectors "
                "(`selectors_re`); only exact `selectors` can be previewed"
            ],
        }

    work = Path(tempfile.mkdtemp(prefix="recipe-preview-"))
    try:
        # The exporter judges the recipe (#3229): its recipe_id, whether it is
        # valid — a missing required field included — and the row and rules it
        # serves. A recipe it does not serve fails loud → state:error,
        # honouring the §4 contract that a config error is a verdict-less
        # error (the catch-all in app.py would otherwise mask it as a 500).
        confd = work / "conf.d"
        confd.mkdir()
        try:
            judged = judge(recipe, tenant, confd, deadline=deadline)
        except tv.DaGuardNotFoundError as exc:
            return _err(f"da-guard not available — cannot judge the recipe: {exc}")
        except tv.DaGuardError as exc:
            # A da-guard that cannot be run, answers unlike da-guard or is
            # older than this core is the service's fault, not the recipe's.
            what = ("the preview's da-guard cannot judge recipes" if exc.binary_fault
                    else "recipe failed to compile")
            return _err(f"{what}: {' '.join(str(exc).split())}")
        except (tv.ParseFailedError, yaml.YAMLError) as exc:
            return _err(f"recipe failed to compile: {' '.join(str(exc).split())}")
        except OSError as exc:
            return _err(f"preview could not write the recipe's tree: {exc}")
        if isinstance(judged, str):
            return _err(judged)
        alertname = judged.alertname

        # Validate the request BEFORE the promtool-availability check, so bad input
        # is reported as an error regardless of whether we can evaluate locally
        # (the "Python Tests" CI job has no promtool on PATH).
        value = (scenario or {}).get("value")
        # threshold is value-crossing → the scenario value is required and must be a
        # bare number (promtool's series grammar reads "1+2" / "5.." as a slope/range
        # → a wrong verdict). absence is presence-based: it needs no value (the metric
        # is simply not emitted), so don't demand one.
        if rtype == "threshold":
            if value is None:
                return _err("scenario.value is required for a threshold preview",
                            alertname=alertname)
            # A JSON number only: a string (even "1500") is refused, so text
            # promtool's series grammar reads differently ("1500\n" is one
            # sample, then stale) never reaches it; bool is an int to Python,
            # and NaN / Infinity are numbers to json.loads (#3611, 2026-11-12).
            if (type(value) not in (int, float)
                    or not math.isfinite(_finite_or_inf(value))):
                return _err(f"scenario.value must be a finite JSON number, got {value!r}",
                            alertname=alertname)
        # absence sizes its eval_time from `window`; one the preview cannot size
        # (beyond a day) fails closed to error — never guess a window → wrong
        # eval_time → a real firing misread as inactive (same class as `for:`).
        if rtype == "absence" and _window_minutes(recipe.get("window")) is None:
            return _err(f"preview cannot size the absence window "
                        f"{recipe.get('window')!r} (expected a Prometheus duration "
                        f"like 10m / 1h30m / 500ms, at most {_MAX_WINDOW_MIN}m)",
                        alertname=alertname)

        if isinstance(_PROMTOOL, engine.NoVerdict):
            return {"alertname": alertname, "supported": True, "states": [],
                    "warnings": ["promtool not available — cannot evaluate locally"]}

        pack_path = work / _PACK_FILE

        # ── render → syntax gate → two opposite groups. Every step's verdict is
        # read from what proves the step ran (_lib_engine's promtool proofs:
        # exit 0 for `check rules`, the JUnit report for `test rules`, never
        # console text — #3611 Q2); anything else is state:error, never a
        # firing / inactive guess. `except OSError`: an unwritable temp dir. ──
        try:
            pack_path.write_text(cc._render([*judged.pack.groups, _LOADED_GROUP]),
                                 encoding="utf-8", newline="\n")
            chk = engine.run(_PROMTOOL, "check-rules", ["check", "rules", pack_path.name],
                             cwd=str(work), timeout=_timeout_left(deadline))
            if isinstance(chk, engine.NoVerdict):
                return _err(f"promtool check rules on the compiled rule: "
                            f"{chk.message()[:300]}", alertname=alertname)
            test_doc = build_preview_test(recipe, tenant, value, judged)
            (work / _TEST_FILE).write_text(test_doc, encoding="utf-8", newline="\n")
            junit = work / "preview_test.junit.xml"
            res = engine.run(_PROMTOOL, "test-rules",
                             ["test", "rules", "--junit", junit.name, _TEST_FILE],
                             cwd=str(work), timeout=_timeout_left(deadline),
                             expect={"junit": str(junit), "cases": {_FIRES, _QUIET}})
        except OSError as exc:
            return _err(f"promtool eval failed: {exc}", alertname=alertname)

        if isinstance(res, engine.NoVerdict):
            return _err(f"promtool eval error: {res.message()[:300]}", alertname=alertname)
        state = _VERDICT.get((res.data[_FIRES], res.data[_QUIET]))
        if state is None:
            return _err("promtool did not evaluate the rule on this input: the "
                        f"{_FIRES!r} and {_QUIET!r} test groups "
                        + ("both failed" if not any(res.data.values()) else "both passed"),
                        alertname=alertname)
        severity = judged.row["severity"]
        thr_value = _fmt_value(judged.row["value"])
        mode = judged.row["labels"].get("mode")
        if rtype == "absence":
            win = recipe.get("window")
            reason = (f"{judged.row['metric']} absent over {win} → would fire ({severity})"
                      if state == "firing"
                      else "simulated absence did not fire — verify the recipe "
                           "compiles to an absence alert for this tenant")
        else:
            op = recipe.get("op", ">")
            reason = (f"value {value} {op} threshold {thr_value}" if state == "firing"
                      else f"value {value} does not cross threshold {thr_value} ({op})")
        return {
            "alertname": alertname,
            "supported": True,
            "states": [{
                "severity": severity,
                "mode": str(mode) if mode else "page",
                "state": state,
                "reason": reason,
            }],
            "warnings": [],
        }
    finally:
        _lib_parse_cache.forget(work / "conf.d")
        shutil.rmtree(work, ignore_errors=True)
