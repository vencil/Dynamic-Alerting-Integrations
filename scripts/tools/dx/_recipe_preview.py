#!/usr/bin/env python3
"""_recipe_preview.py — recipe would-fire preview core (#657 P2).

Given ONE ADR-024 custom-alert recipe + a scenario value, answer "would it
fire?" by going through the SAME authoritative engine the platform uses —
`compile_custom_alerts.build_pack` + `promtool` — never re-implementing eval
(the two-eval-homes rule; see docs/design/recipe-would-fire-preview.md).

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

Eval mechanism (§5.2): `promtool test rules` is an ASSERT tool, so we run an
INVERTED assert (synthetic input + `exp_alerts: []`): returncode 0 → nothing
fired (inactive); returncode != 0 → it fired. A compile error must NOT be
mislabeled as firing, so we gate with `build_pack` exception handling +
`promtool check rules` (syntax) BEFORE the inverted-assert (§5.2 layering).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
import compile_custom_alerts as cc  # noqa: E402
from custom_alerts import shape  # noqa: E402

# `threshold` covers >, >=, <, <=, == (all value-crossing, flat series). `absence`
# is presence-based — fires where a DECLARING tenant's metric had no sample over
# the window — so a "don't emit the metric" series reproduces it exactly. The
# remaining (rate/ratio/forecast/p99_latency) stay deferred (need a populated
# lookback). Per-type gating (§7): an unsupported type returns supported:false
# WITHOUT a compile, so it is never mislabeled firing/error.
SUPPORTED_RECIPES_MVP = frozenset({"threshold", "absence"})

# `for:` is enum-bounded (shape.ALLOWED_FOR). Map to minutes to size the
# synthetic series + pick an eval_time PAST the pending window.
_FOR_MINUTES = {"0s": 0, "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}

# `window` is a Prometheus/Go duration, NOT enum-bounded like `for:` — so PARSE it
# (not a fixed map) to size the absence eval_time. It can be COMPOUND ("1h30m") and
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


def _window_minutes(window):
    """Prometheus duration (compound + sub-second ok) → whole minutes (ceil; min 1).
    INTEGER arithmetic throughout. None on a malformed / non-positive / absurdly
    large (> _MAX_WINDOW_MIN) window — the preview can't synthesize a series that
    long, and a recipe with such a window still deploys (the preview just abstains)."""
    w = str(window or "").strip()
    if not _DUR_FULL_RE.match(w):
        return None
    ns = sum(int(n) * _DUR_UNIT_NS[u] for n, u in _DUR_TOKEN_RE.findall(w))
    if ns <= 0:
        return None
    minutes = -(-ns // _NS_PER_MIN)   # integer ceil-div: no float → no overflow
    return minutes if minutes <= _MAX_WINDOW_MIN else None

_PROMTOOL = shutil.which("promtool")

_TIMEOUT = 60


def _labels(d):
    """Render a Prometheus label set `{k="v", ...}` (insertion order).

    Values are escaped with the compiler's own `shape._escape_value`, so a
    selector value containing a quote, backslash, or newline yields the SAME
    label literal the compiled rule matches against — otherwise the synthetic
    series wouldn't match (false "inactive") or would be an unparseable series.
    """
    return "{" + ", ".join(
        f'{k}="{shape._escape_value(str(v))}"' for k, v in d.items()
    ) + "}"


def build_preview_test(recipe, tenant, value, slug):
    """Build a promtool test (YAML str) for a supported recipe — inverted assert.

    POLYMORPHIC by recipe type (a flat series can't fake every shape):
      - threshold (>, >=, <, <=, ==): the observed metric @ the scenario `value`
        (+ selector labels) + `user_threshold` @ the configured threshold +
        a constant `tenant_metadata_info`; flat, held past `for:`.
      - absence: emit ONLY the declaration (`user_threshold`, which the compiler
        records as `custom:threshold:{id}`) + `tenant_metadata_info`, and DO NOT
        emit the metric. The rule is `custom:threshold:{id} unless on(tenant)
        count by(tenant)(count_over_time(metric[window]) > 0)`, so an absent
        metric leaves the `unless` arm empty → the declaring tenant fires (the
        absence.yaml firing case). `value` is unused. eval past `window` + `for:`.
    Returns (yaml_text, severity, mode, threshold_value).
    """
    rtype = recipe.get("recipe")
    thr_value, severity = shape.parse_threshold(recipe["threshold"])
    name = recipe["name"]
    mode = recipe.get("mode")
    for_min = _FOR_MINUTES[str(recipe.get("for", "1m"))]   # caller pre-validates membership

    ut_labels = {
        "component": "custom",
        "metric": recipe["metric"],
        "recipe_id": slug,
        "tenant": tenant,
        "severity": severity,
        "name": name,
    }
    if mode:
        ut_labels["mode"] = str(mode)

    def _md(n):
        # constant tenant_metadata_info for the group_left enrichment (both shapes)
        return (
            f"      - series: 'tenant_metadata_info{{tenant=\"{tenant}\", "
            f"runbook_url=\"http://rb/{tenant}\", owner=\"team-{tenant}\", tier=\"gold\"}}'\n"
            f"        values: '1x{n}'\n"
        )

    if rtype == "absence":
        # eval must clear BOTH the absence detection window AND `for:`. The metric
        # is INTENTIONALLY absent (no series), so count_over_time(metric[window])
        # is empty → the rule's `unless on(tenant)` keeps the declaring tenant →
        # fires. `value` is irrelevant to a presence check.
        win_min = _window_minutes(recipe["window"])   # caller pre-validates (not None)
        eval_min = win_min + for_min + 5
        n = eval_min + 5
        input_series = (
            f"      - series: 'user_threshold{_labels(ut_labels)}'\n"
            f"        values: '{thr_value}x{n}'\n"
            + _md(n)
        )
    else:
        # threshold / == : a flat constant series at the scenario value, held long
        # enough to clear `for:`.
        eval_min = for_min + 5
        n = for_min + 10
        selectors = {str(k): v for k, v in (recipe.get("selectors") or {}).items()}
        metric_labels = {"tenant": tenant, **selectors}
        input_series = (
            f"      - series: '{recipe['metric']}{_labels(metric_labels)}'\n"
            f"        values: '{value}x{n}'\n"
            f"      - series: 'user_threshold{_labels(ut_labels)}'\n"
            f"        values: '{thr_value}x{n}'\n"
            + _md(n)
        )

    doc = (
        "rule_files:\n"
        "  - rule-pack-custom-alerts.yaml\n"
        "evaluation_interval: 1m\n"
        "tests:\n"
        "  - interval: 1m\n"
        "    input_series:\n"
        + input_series +
        "    alert_rule_test:\n"
        f"      - eval_time: {eval_min}m\n"
        f"        alertname: Custom_{slug}\n"
        "        exp_alerts: []\n"
    )
    return doc, severity, mode, thr_value


def _err(reason, alertname=None):
    return {"alertname": alertname, "supported": True,
            "states": [{"state": "error", "reason": reason}], "warnings": []}


def classify_promtool_result(returncode, output):
    """Map a promtool `test rules` (run with `exp_alerts: []`) result to a state.

    rc != 0 alone is NOT "firing": promtool exits non-zero for OOM/kill, a
    missing binary, or a test-file parse error too. Treating any of those as
    "firing" would be a false positive (the exact false-confidence the design
    forbids). So FIRING requires the alert-mismatch signature promtool prints
    when an alert fires against our empty expectation — `FAILED:` plus a
    non-empty `got:` block (verified verbatim on promtool 3.12.0). Anything
    else with rc != 0 is an infrastructure/parse error → `error`.

      rc == 0                      → inactive (nothing fired)
      rc != 0 + FAILED: + got:[    → firing
      rc != 0 otherwise            → error
    """
    if returncode == 0:
        return "inactive"
    if "FAILED:" in output and "got:[" in output:
        return "firing"
    return "error"


def preview_recipe(recipe, tenant, scenario):
    """Would-fire preview for ONE recipe. Returns the §4 contract dict:
    {alertname, supported, states:[{severity, mode, state, reason}], warnings}.

    state ∈ firing | inactive | error. Per-type gating: unsupported recipe
    types return supported:false (no compile). promtool absent → supported:true
    with a warning and no states (cannot evaluate locally).
    """
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

    # Compute the slug via the compiler's OWN function (zero cross-language
    # drift, §5.3). A structurally invalid recipe (RecipeError) OR one missing a
    # required key (KeyError, e.g. no `metric` — recipe_id does `inst["metric"]`)
    # fails loud here → state:error, honouring the §4 contract that a config error
    # is a verdict-less error (the catch-all in app.py would otherwise mask it as a
    # 500). Pre-existing on the threshold path too; caught here for both.
    try:
        slug = shape.recipe_id(recipe)
    except shape.RecipeError as exc:
        return _err(str(exc))
    except KeyError as exc:
        return _err(f"recipe is missing required field {exc}")

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
                        alertname=f"Custom_{slug}")
        try:
            float(value)
        except (TypeError, ValueError):
            return _err(f"scenario.value must be numeric, got {value!r}",
                        alertname=f"Custom_{slug}")
    # `for:` is enum-bounded by shape.ALLOWED_FOR, but the preview must also be
    # able to SIZE the synthetic series from it. If the two ever drift (a new
    # ALLOWED_FOR value unmapped in _FOR_MINUTES), fail closed to error rather
    # than silently shrink the window to 1m → wrong eval_time/length → a real
    # firing misread as inactive (CodeRabbit #873).
    _for = str(recipe.get("for", "1m"))
    if _for not in _FOR_MINUTES:
        return _err(f"preview cannot size the series for for-window {_for!r} "
                    f"(sync _FOR_MINUTES with shape.ALLOWED_FOR)",
                    alertname=f"Custom_{slug}")
    # absence sizes its eval_time from `window` (a Go duration, NOT enum-bounded).
    # If it can't be parsed, fail closed to error — never guess a window → wrong
    # eval_time → a real firing misread as inactive (same class as the for guard).
    if rtype == "absence" and _window_minutes(recipe.get("window")) is None:
        return _err(f"preview cannot size the absence window "
                    f"{recipe.get('window')!r} (expected a Prometheus duration "
                    f"like 10m / 1h30m / 500ms)",
                    alertname=f"Custom_{slug}")

    if _PROMTOOL is None:
        return {"alertname": f"Custom_{slug}", "supported": True, "states": [],
                "warnings": ["promtool not available — cannot evaluate locally"]}

    work = Path(tempfile.mkdtemp(prefix="recipe-preview-"))
    try:
        confd = work / "conf.d"
        confd.mkdir()
        (confd / f"{tenant}.yaml").write_text(
            yaml.safe_dump({"tenants": {tenant: {"_custom_alerts": [recipe]}}},
                           sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

        # ── compile (errors → state:error, never mislabeled firing) ──
        try:
            pack = cc.build_pack(confd)
        except Exception as exc:  # RecipeError / CustomAlertConfigError / loader errors
            return _err(f"recipe failed to compile: {exc}", alertname=f"Custom_{slug}")

        pack_path = work / "rule-pack-custom-alerts.yaml"

        # ── render → syntax gate → inverted-assert. Every failure here is
        # fail-closed to state:error rather than ever mislabel an infra / IO /
        # timeout failure as firing (§5.2 layering). `except OSError` catches
        # promtool vanishing after the `which` probe or an unwritable temp dir,
        # so the {state:error} contract holds even then. ──
        try:
            pack_path.write_text(cc._render(pack["groups"]), encoding="utf-8")
            chk = subprocess.run(
                [_PROMTOOL, "check", "rules", pack_path.name],
                cwd=work, capture_output=True, text=True, timeout=_TIMEOUT,
            )
            if chk.returncode != 0:
                return _err(f"compiled rule failed promtool check: "
                            f"{(chk.stderr or chk.stdout).strip()[:300]}",
                            alertname=f"Custom_{slug}")
            test_doc, severity, mode, thr_value = build_preview_test(recipe, tenant, value, slug)
            (work / "preview_test.yaml").write_text(test_doc, encoding="utf-8")
            res = subprocess.run(
                [_PROMTOOL, "test", "rules", "preview_test.yaml"],
                cwd=work, capture_output=True, text=True, timeout=_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return _err(f"promtool timed out (>{_TIMEOUT}s)", alertname=f"Custom_{slug}")
        except OSError as exc:
            return _err(f"promtool eval failed: {exc}", alertname=f"Custom_{slug}")

        out = (res.stdout or "") + (res.stderr or "")
        state = classify_promtool_result(res.returncode, out)
        if state == "error":
            return _err(f"promtool eval error (rc={res.returncode}): "
                        f"{out.strip()[:300] or 'no output'}", alertname=f"Custom_{slug}")
        if rtype == "absence":
            win = recipe.get("window")
            reason = (f"{recipe['metric']} absent over {win} → would fire ({severity})"
                      if state == "firing"
                      else "simulated absence did not fire — verify the recipe "
                           "compiles to an absence alert for this tenant")
        else:
            op = recipe.get("op", ">")
            verb = "==" if op == "==" else op
            reason = (f"value {value} {verb} threshold {thr_value}" if state == "firing"
                      else f"value {value} does not cross threshold {thr_value} ({op})")
        return {
            "alertname": f"Custom_{slug}",
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
        shutil.rmtree(work, ignore_errors=True)
