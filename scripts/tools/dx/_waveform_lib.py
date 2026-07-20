#!/usr/bin/env python3
"""_waveform_lib.py — fault-waveform pack 合成核心（ADR-030 決策層驗證 PR-1，純函式庫）

Pure library behind waveform_compile.py: load / validate / synthesize /
variants / two materializations / readback rendering. No printing, no file
writes (the CLI owns I/O). Deterministic by construction:

  * seeded ``random.Random`` instances only — never wall clock;
  * ``T0 = 1_700_000_000`` fixed epoch, ``STEP = 30`` seconds (explicit
    semantic constants, mirrored into every materialization's metadata);
  * fan-out labels derived from the series index (``series="f01"``), never
    uuid;
  * every series carries ``waveform_signature="<signature_index>"`` +
    ``waveform_variant`` labels (synth-time identity keys; PR-2 attribution);
  * gaussian noise is an OWNED Box-Muller over ``rng.random()`` so bitwise
    reproducibility does not depend on stdlib ``gauss`` internals.

Compile semantics (ADR-030 D2 / R2-1, PR-1 design doc §2-§3):

  * ``metric_kind: gauge``   — levels used directly.
  * ``metric_kind: counter`` — SME levels are RATES (per-second); the
    compiler integrates them into a monotone cumulative sample series
    (negative instantaneous rates from noise/dips are clamped to 0 and the
    clamp is recorded in ``auto_adjustments``). Counter-reset variants are
    NOT generated in v1.
  * ``metric_kind: boolean`` — noise exemption (auto-justification recorded
    in metadata); the noise/oscillation variants are re-cast as
    **flapping** / **staleness-absence** (series ends early) instead.

Variants are always generated (no off switch, R2-1):

  base / noise (or flapping) / oscillation (or absence) / fan-out.

``expects`` semantics (P6): variants grounded in SME-declared behaviour
(base + noise + declared dips + fan-out) inherit ``must_detect``; purely
mechanical probes (full-depth dips generated despite ``dips_back: false``,
absence despite ``agent_keeps_reporting: true``) are tagged ``probe`` —
reported but never gating the verdict. Companion series are ``companion``.

Materializations:
  (a) promtool fixture fragment — ``values:`` notation, one token per
      sample, ``_`` for gaps. Reference only, NOT the catch-rate
      authority. Structurally cannot express jitter; when a signature
      declares jitter the fragment carries an explicit "不含 jitter"
      annotation.
  (b) Prometheus import text lines ``metric{labels} value ts_ms`` with
      absolute millisecond timestamps — the catch-rate authority; jitter
      applies here.
"""
from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

# ── Explicit semantic constants ──────────────────────────────────────
T0 = 1_700_000_000          # fixed epoch origin (seconds); sample i sits at T0 + i*STEP
STEP = 30                    # sampling interval in seconds (pinned per #968)
DEFAULT_SEED = 1             # CLI default seed (fixed, never wall-clock)
DEFAULT_FANOUT = 3           # fan-out series count default
LEAD_STEPS = 10              # normal-level lead-in samples before onset
TAIL_STEPS = 10              # samples after recovery (or at fault for plateau)
PROBE_DIP_PERIOD_STEPS = 4   # mechanical full-depth probe dip cadence (dips_back=false)
GAUSS_SIGMA_DIV = 3.0        # wobble is a hard bound; gaussian sigma = wobble/3, clamped

SME_FIELDS = frozenset({
    "description", "source", "normal_level", "fault_level",
    "onset_duration", "hold_duration", "typical_wobble",
    "dips_back", "dip_detail", "agent_keeps_reporting", "must_detect",
})
PLATFORM_FIELDS = frozenset({
    "metric", "metric_kind", "unit", "companion_series", "shape_class",
    "noise_kind", "time_axis", "fault_class", "labels",
    # pack-level platform bookkeeping
    "id", "domain", "author_role", "readback_signed_off",
    "independent_of_rule_conversion", "pack", "signatures",
})

_DUR_FULL_RE = re.compile(r"^([0-9]+(?:s|m|h|d|w))+$")
_DUR_PART_RE = re.compile(r"([0-9]+)(s|m|h|d|w)")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


class WaveformError(Exception):
    """Base class for waveform library errors."""


class WaveformInputError(WaveformError):
    """Unusable input (malformed YAML / bad duration / unreadable file) — caller error."""


# ── Small pure helpers ───────────────────────────────────────────────

def parse_duration(text: Any) -> int:
    """Parse a promtool-style duration string (``30s``/``5m``/``2h``/``1h30m``) to seconds."""
    if not isinstance(text, str) or not _DUR_FULL_RE.match(text):
        raise WaveformInputError(f"invalid duration string: {text!r} (expected e.g. 30s / 5m / 2h)")
    return sum(int(n) * _UNIT_SECONDS[u] for n, u in _DUR_PART_RE.findall(text))


def load_pack(path: str) -> dict:
    """Load a waveform pack YAML file. Raises WaveformInputError on unreadable/malformed input."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise WaveformInputError(f"cannot read/parse pack YAML {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WaveformInputError(
            f"pack YAML {path}: top-level document must be a mapping, got {type(data).__name__}")
    return data


def _fmt(v: float) -> str:
    """Deterministic compact number formatting shared by both materializations."""
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    if s in ("", "-0"):
        return "0"
    return s


def _fmt_labels(labels: dict) -> str:
    """Render a label dict as a Prometheus label set, keys sorted for determinism."""
    if not labels:
        return ""
    parts = []
    for k in sorted(labels):
        val = str(labels[k]).replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'{k}="{val}"')
    return "{" + ",".join(parts) + "}"


def _gauss01(rng: random.Random) -> float:
    """Owned Box-Muller standard normal over rng.random() (bitwise-stable across
    Python versions — stdlib gauss internals stay out of the determinism claim)."""
    u1 = 1.0 - rng.random()  # (0, 1]
    u2 = rng.random()
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def _noise_sample(rng: random.Random, wobble: float, noise_kind: str) -> float:
    """One noise sample. typical_wobble is a HARD amplitude bound (testable):
    gaussian draws sigma=wobble/3 then clamps to ±wobble; uniform draws ±wobble."""
    if wobble <= 0:
        return 0.0
    if noise_kind == "uniform":
        return rng.uniform(-wobble, wobble)
    raw = _gauss01(rng) * (wobble / GAUSS_SIGMA_DIV)
    return max(-wobble, min(wobble, raw))


# ── Validation (schema wrapper + tiered messages) ────────────────────

def classify_field(name: str) -> str:
    """Map a schema field name to its ownership tier: 'sme' | 'platform' | 'other'."""
    if name in SME_FIELDS:
        return "sme"
    if name in PLATFORM_FIELDS:
        return "platform"
    return "other"


_REQUIRED_MSG_RE = re.compile(r"'([^']+)' is a required property")


def _error_field(err: Any) -> str:
    """Best-effort field name for a jsonschema ValidationError."""
    m = _REQUIRED_MSG_RE.search(err.message or "")
    if m:
        return m.group(1)
    for part in reversed(list(err.absolute_path)):
        if isinstance(part, str):
            return part
    return ""


def validate_pack(pack: dict, schema: dict, validator_module: Any) -> list[dict]:
    """Validate *pack* against *schema* using the injected jsonschema module.

    Returns a list of issue dicts ``{"tier": ..., "field": ..., "message": ...}``
    where SME-field gaps are prefixed 「退回 SME」 and platform-field gaps
    「平台補填」 (two-tier hard split, PR-1 design doc §2). Empty list == valid.
    The module is injected so the import stays lazy in the CLI (--help must
    work in a jsonschema-less env; see check_confd_schema.py precedent).
    """
    validator = validator_module.Draft7Validator(schema)
    issues: list[dict] = []
    for err in sorted(validator.iter_errors(pack), key=lambda e: list(e.absolute_path)):
        fld = _error_field(err)
        path = "/".join(str(p) for p in err.absolute_path) or "(root)"
        tier = classify_field(fld)
        if fld == "independent_of_rule_conversion":
            msg = (f"pack.independent_of_rule_conversion 必須為 true"
                   f"（盲寫 attestation，ADR-030 D2 反循環守則）: {err.message}")
            issues.append({"tier": "platform", "field": fld,
                           "message": f"平台補填: {path}: {msg}"})
        elif tier == "sme":
            issues.append({"tier": "sme", "field": fld,
                           "message": f"退回 SME: {path}: {err.message}"})
        elif tier == "platform":
            issues.append({"tier": "platform", "field": fld,
                           "message": f"平台補填: {path}: {err.message}"})
        else:
            issues.append({"tier": "other", "field": fld,
                           "message": f"schema violation: {path}: {err.message}"})
    return issues


def selftest_gate_issues(pack: dict, allow_selftest: bool) -> list[dict]:
    """`source: self-test-seed` is compiler-gated: without --allow-selftest it is a
    violation (C5 governance — seeds must never leak into catch-rate material)."""
    if allow_selftest:
        return []
    issues = []
    for i, sig in enumerate(pack.get("signatures") or []):
        if isinstance(sig, dict) and sig.get("source") == "self-test-seed":
            issues.append({
                "tier": "governance", "field": "source",
                "message": (f"signatures/{i}: source: self-test-seed 只供工具自測，"
                            f"禁止進入 catch-rate 素材；若確為自測用途請帶 --allow-selftest"),
            })
    return issues


# Declared exporter scale -> the closed interval the SME's levels must fall in.
# Units without a natural bound (bytes / count / seconds / per_second) are absent
# on purpose: any non-negative magnitude is legitimate, so a bound would be noise.
_UNIT_BOUNDS = {
    "ratio_0_to_1": (0.0, 1.0),
    "percent_0_to_100": (0.0, 100.0),
    "boolean": (0.0, 1.0),
}


def _sig_levels(sig: dict):
    """(label, value) for the magnitudes ``unit`` governs — i.e. those on the
    signature's OWN metric only.

    Includes ``typical_wobble`` (a noise AMPLITUDE, but expressed in the metric's
    own unit — a ±2 wobble on a 0–1 gauge swamps the signal) and ``min_value``
    (a clamp floor applied directly to synthesized samples), because both are
    load-bearing in synthesis and both silently corrupt the waveform when they
    are authored on the wrong scale.

    ⚠️ ``companion_series`` levels are deliberately EXCLUDED: a companion is a
    DIFFERENT metric with its own scale (a ratio signature's denominator is
    typically a rate or a count), so applying the parent's ``unit`` to it is a
    category error and produces false positives. Checking companions would need a
    per-companion ``unit`` field — documented limitation, not built yet.
    """
    for key in ("normal_level", "fault_level", "typical_wobble", "min_value"):
        v = sig.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            yield key, float(v)
    dip = sig.get("dip_detail")
    if isinstance(dip, dict):
        v = dip.get("depth")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            yield "dip_detail.depth", float(v)


def unit_scale_issues(pack: dict) -> list[dict]:
    """Fail loud when a signature's levels contradict its declared ``unit``.

    WHY (ADR-030 field finding): a scale mismatch is a SILENT false-negative. An
    SME answers "the hit ratio drops from 97% to 72%" while the exporter emits a
    0–1 ratio and the rule threshold is 0.95 — the injected 97 never crosses it,
    the alert never fires, and the report reads as a coverage gap that is really a
    unit bug. This was hit for real on db2_bufferpool_hit_ratio; the reconciliation
    was done by hand, which does not scale to an air-gapped SME filling the template
    without PromQL knowledge.

    ``unit`` is OPTIONAL: absent = unchecked (backward compatible). Declaring it is
    what buys the guard.

    ⚠️ DELIBERATE NON-DETECTION — the REVERSE mis-scale (declaring
    ``percent_0_to_100`` while answering in 0–1 ratio form) is NOT flagged. It is
    irreducibly ambiguous: ``0.2`` under a percent unit is equally consistent with a
    genuine 0.2%-scale metric (error rate, packet loss, 5xx ratio — sub-1% ranges are
    common and legitimate) and with a ratio-form answer. A heuristic here fails loud
    on correct input, and the only escape is deleting ``unit``, which disarms the
    guard entirely on exactly the metric class it exists for. So the guard covers the
    UNAMBIGUOUS direction only: a value outside the declared unit's range is always
    wrong. The motivating db2_bufferpool_hit_ratio case is in that direction.
    """
    issues = []
    for i, sig in enumerate(pack.get("signatures") or []):
        if not isinstance(sig, dict):
            continue
        unit = sig.get("unit")
        if unit is None:
            continue
        # metric_kind ↔ unit compatibility. For `counter`, the SME's levels are
        # RATES (per-second), so a fraction-shaped unit does not describe them —
        # applying a [0,1] / [0,100] bound to "5 events/sec" is the same category
        # error that companion levels are excluded for. Reject the combination
        # explicitly instead of silently bounding a rate.
        if sig.get("metric_kind") == "counter" and unit in ("ratio_0_to_1",
                                                            "percent_0_to_100"):
            issues.append({
                "tier": "platform", "field": "unit",
                "message": (f"平台補填: signatures/{i}: metric_kind=counter 的水位是"
                            f"「速率（每秒）」，與 unit={unit}（分數型尺度）不相容——"
                            f"對速率套用 [0,1]/[0,100] 界是類別錯誤。counter 請用 "
                            f"per_second / count，分數型 unit 僅適用 gauge"),
            })
            continue
        bounds = _UNIT_BOUNDS.get(unit)
        if bounds is None:
            continue
        lo, hi = bounds
        for label, val in _sig_levels(sig):
            if not (lo <= val <= hi):
                issues.append({
                    "tier": "platform", "field": "unit",
                    "message": (f"平台補填: signatures/{i}: unit={unit} 宣告的範圍是 "
                                f"[{lo}, {hi}]，但 {label}={val} 超出 → 尺度不符。"
                                f"SME 若以另一種尺度作答（如 0-100% vs 0-1 比例），"
                                f"注入值將永遠跨不過該尺度的告警閾值＝靜默漏報；"
                                f"請確認 exporter 實際輸出的尺度後改正 unit 或數值"),
                })
        if unit == "boolean":
            for label, val in _sig_levels(sig):
                if val not in (0.0, 1.0):
                    issues.append({
                        "tier": "platform", "field": "unit",
                        "message": (f"平台補填: signatures/{i}: unit=boolean 但 "
                                    f"{label}={val} 非 0/1"),
                    })
    return issues


def semantic_issues(pack: dict) -> list[dict]:
    """Cross-field semantic checks JSON Schema (draft-07) cannot express. Runs
    after schema validation; defensively skips signatures whose fields are
    missing/mistyped (the schema layer catches those)."""
    issues = unit_scale_issues(pack)
    for i, sig in enumerate(pack.get("signatures") or []):
        if not isinstance(sig, dict):
            continue
        nl, fl = sig.get("normal_level"), sig.get("fault_level")
        if (isinstance(nl, (int, float)) and not isinstance(nl, bool)
                and isinstance(fl, (int, float)) and not isinstance(fl, bool)
                and nl == fl):
            issues.append({
                "tier": "sme", "field": "fault_level",
                "message": (f"退回 SME: signatures/{i}: normal_level == fault_level "
                            f"({nl}) → 無故障可注入，此 must_detect 序列會稀釋 "
                            f"catch-rate 分母；請填與正常水位有落差的故障水位"),
            })
    return issues


# ── Synthesis ────────────────────────────────────────────────────────

@dataclass
class Series:
    """One output series with per-series provenance metadata."""
    metric: str
    labels: dict
    samples: list  # list[Optional[float]] — None == gap (dropout)
    variant: str
    expects: str   # must_detect | informational | probe | companion
    signature_index: int
    fault_class: str
    source: str
    jitter_s: float = 0.0
    truncated: bool = False
    auto_adjustments: list = field(default_factory=list)
    rng_key: str = ""
    # 可偵測故障窗（秒、相對窗起點——sample i 在 i*STEP，與 PR-2 fire_offset_s 同軸）。
    # (start_s, end_s)；end_s=None = 開放至觀測窗尾（staleness_absence）；None = 無法
    # 定義（截斷吃光 hold 段，auto_adjustments 留痕）。語義詳 _fault_window_s。
    fault_window: Optional[tuple] = None
    # fault-hold 段起點（秒；value 變體 = fw[0]*STEP，較窗下界 onset 起點晚）。PR-3
    # scorer 用來標記「規則在 onset 段就開火」的 early-onset 過敏 hit（揭露不 gate）。
    # staleness_absence → 設等於窗下界（early_onset 對 absence 不適用、令永不觸發）；
    # fault_window=None → None（無窗可標）。additive 欄，語義詳 _hold_start_s。
    hold_start_s: Optional[float] = None


def _inherit_expects(sig: dict) -> str:
    return "must_detect" if sig.get("must_detect") else "informational"


def _fault_window_s(variant: str, fw: tuple[int, int], n_samples: int,
                    notes: list) -> Optional[tuple]:
    """Per-variant 可偵測故障窗（秒、相對窗起點；sample i 在 i*STEP——與 PR-2
    inject 報告的 ``fire_offset_s`` 同一時間軸，PR-3 temporal-match 的血緣來源）。

    * value 變體（base/noise/oscillation/fanout/flapping）：``(onset 起點,
      fault-hold 末樣本)``。下界取 **onset 起點**（LEAD_STEPS*STEP）而非 hold
      起點——訊號離開 normal 即故障開始，for:-gated 告警常在 onset 段就開火
      （提早接住），若下界取 hold 起點會把最典型的正確偵測誤判為 miss。
      staleness_tail 截斷吃進 hold 段時上界 clamp 到最後存活樣本；吃光整個
      hold → None + auto_adjustments 留痕（no-silent-caps）。
    * staleness_absence：``(agent 死亡點, None)``——下界 = **截斷後序列結尾**
      （n_samples*STEP，非 hold 起點常數）：absence 變體可能再被 time_axis 的
      staleness_tail 二次截斷，agent 實際死亡點是二次截斷後的序列結尾；無 tail
      時 n_samples == hold 起點、語義不變。可偵測訊號是「absence」，自死亡點
      起持續到觀測窗尾，上界開放（scorer 以該報告的窗長收尾）。absence 偵測
      天然有 staleness 遲滯（#968：~1-2 個 scrape interval），屬量測對象的
      真實屬性，由容差吸收、不在窗內漂白。
    * probe / companion：同其 value 窗（informational——不入 catch-rate 分母，
      窗僅供對帳呈現）。
    """
    if variant == "staleness_absence":
        # 下界對齊「實際」死亡點：staleness_tail 對 absence 變體是二次截斷，
        # 用 hold 起點常數會把窗下界定在資料還在的區段 → 假 FN（血緣破口）。
        return (n_samples * STEP, None)
    end_idx = min(fw[1], n_samples - 1)
    if end_idx < fw[0]:
        notes.append(
            "fault_window: staleness_tail 截斷吃光 fault-hold 段 → 值域故障窗"
            "無法定義（記 None；下游 scorer 須顯性列出、不得靜默計 hit/miss）")
        return None
    return (LEAD_STEPS * STEP, end_idx * STEP)


def _hold_start_s(variant: str, fw: tuple[int, int],
                  fault_window: Optional[tuple]) -> Optional[float]:
    """fault-hold 段起點的秒級位移（PR-3 early-onset 過敏標記血緣；G-2 additive）。

    * value 變體（base/noise/oscillation/fanout/flapping/probe）：``fw[0]*STEP``——
      hold 段第一個樣本，較窗下界 onset 起點（LEAD_STEPS*STEP）晚。scorer 標記
      fire_offset ∈ [onset 起點, hold 起點) 的「規則在故障才成形就開火」過敏 hit。
    * staleness_absence：設 = 窗下界（``fault_window[0]``）——early_onset 對 absence
      不適用（可偵測訊號自死亡點起），令永不觸發標記。
    * fault_window=None（截斷吃光 hold 段）：None——無窗可標。
    """
    if fault_window is None:
        return None
    if variant == "staleness_absence":
        return float(fault_window[0])   # == 窗下界，early_onset 永不觸發
    return float(fw[0] * STEP)


def _base_waveform(sig: dict) -> tuple[list[float], tuple[int, int]]:
    """Clean envelope per shape_class. Returns (values, fault_window) where
    fault_window = (first_index, last_index) of the pure fault-hold segment.

    step:    normal → fault in ONE sample (onset collapsed), hold, instant recovery.
    ramp:    linear onset over onset_duration, hold, linear recovery (same slope).
    spike:   linear onset, hold, instant recovery.
    plateau: linear onset, then stays at fault_level to the end (no recovery).
    """
    normal = float(sig["normal_level"])
    fault = float(sig["fault_level"])
    shape = sig["shape_class"]
    onset_steps = 1 if shape == "step" else max(
        1, round(parse_duration(sig["onset_duration"]) / STEP))
    hold_steps = max(1, round(parse_duration(sig["hold_duration"]) / STEP))

    values: list[float] = [normal] * LEAD_STEPS
    for k in range(1, onset_steps + 1):
        values.append(normal + (fault - normal) * k / onset_steps)
    fault_start = len(values)
    values.extend([fault] * hold_steps)
    fault_end = len(values) - 1
    if shape == "ramp":
        for k in range(1, onset_steps + 1):
            values.append(fault + (normal - fault) * k / onset_steps)
        values.extend([normal] * TAIL_STEPS)
    elif shape == "plateau":
        values.extend([fault] * TAIL_STEPS)
    else:  # spike / step: instant recovery
        values.extend([normal] * TAIL_STEPS)
    return values, (fault_start, fault_end)


def _apply_noise(values: list[float], rng: random.Random, wobble: float,
                 noise_kind: str) -> list[float]:
    return [v + _noise_sample(rng, wobble, noise_kind) for v in values]


def _apply_declared_dips(values: list[float], fw: tuple[int, int],
                         sig: dict) -> tuple[list[float], Optional[str]]:
    """dips_back=true: SME-declared dips — depth toward normal_level (direction-
    neutral, never overshooting normal), cadence from dip_detail.period.

    Dips fall on interior fault-window samples only (never the first), so the
    fault onset is a clean fault_level — otherwise every onset/for-duration rule
    sees a systematically dip-contaminated first sample.
    Returns (values, clamp_note); clamp_note is set (D8.4 no-silent-caps) when
    the declared depth would overshoot normal_level and was clamped."""
    normal = float(sig["normal_level"])
    fault = float(sig["fault_level"])
    depth = float(sig["dip_detail"]["depth"])
    period_steps = max(2, round(parse_duration(sig["dip_detail"]["period"]) / STEP))
    direction = 1.0 if normal > fault else -1.0
    raw_dip = fault + direction * depth
    # clamp: dip moves TOWARD normal but never past it
    dip_value = min(raw_dip, normal) if direction > 0 else max(raw_dip, normal)
    note = None
    if dip_value != raw_dip:
        note = (f"oscillation: declared dip depth {depth} overshoots normal_level "
                f"{normal} (raw dip {raw_dip}) → clamped to normal_level; "
                f"check dip_detail.depth units")
    out = list(values)
    for i in range(fw[0], fw[1] + 1):
        if (i - fw[0]) > 0 and (i - fw[0]) % period_steps == 0:
            out[i] = dip_value
    return out, note


def _apply_probe_dips(values: list[float], fw: tuple[int, int], sig: dict) -> list[float]:
    """dips_back=false: mechanical full-depth probe — dips all the way back to
    normal_level (P5: any meaningful threshold lies in (normal, fault], so a
    full-depth dip is guaranteed to cross it — threshold-blind, anti-tautology).

    Interior fault-window samples only (never the first) — keeps the fault onset
    a clean fault_level so onset/for-duration coverage isn't biased."""
    normal = float(sig["normal_level"])
    out = list(values)
    for i in range(fw[0], fw[1] + 1):
        if (i - fw[0]) > 0 and (i - fw[0]) % PROBE_DIP_PERIOD_STEPS == 0:
            out[i] = normal
    return out


def _apply_flapping(values: list[float], fw: tuple[int, int], sig: dict) -> list[float]:
    """boolean: flapping variant — alternate fault/normal every sample inside
    the fault window (realistic boolean perturbation replacing analog noise)."""
    normal = float(sig["normal_level"])
    fault = float(sig["fault_level"])
    out = list(values)
    for i in range(fw[0], fw[1] + 1):
        out[i] = fault if (i - fw[0]) % 2 == 0 else normal
    return out


def _integrate_counter(rates: list[float]) -> tuple[list[float], int]:
    """counter semantics: levels are per-second RATES → cumulative monotone
    samples. Negative instantaneous rates (noise/dip artifacts) clamp to 0.
    Returns (samples, clamp_count)."""
    cum = 0.0
    out: list[float] = []
    clamped = 0
    for r in rates:
        if r < 0:
            r = 0.0
            clamped += 1
        cum += r * STEP
        out.append(round(cum, 6))
    return out, clamped


def _dropout_indices(pattern: Any, length: int) -> set[int]:
    if pattern is None:
        return set()
    if isinstance(pattern, str):
        n = int(pattern.split(":", 1)[1])
        return {i for i in range(1, length) if i % n == 0}
    return {i for i in pattern if 0 <= i < length}


def _apply_time_axis(samples: list, time_axis: dict) -> tuple[list, int, bool]:
    """Apply dropout (None gaps) + staleness_tail truncation.
    Returns (samples, gap_count, truncated)."""
    out = list(samples)
    truncated = False
    tail = time_axis.get("staleness_tail")
    if tail:
        cut = max(1, math.ceil(parse_duration(tail) / STEP))
        if cut >= len(out):
            # fail-loud (D8.4 no-silent-caps): a staleness_tail >= the whole
            # series would silently no-op, so the SME-declared agent-death is
            # never actually injected. Force a fix rather than ship a
            # must_detect series that quietly lacks its staleness.
            raise WaveformInputError(
                f"staleness_tail ({tail}) truncates the entire {len(out)}-sample "
                f"series (cut={cut}) — shorten staleness_tail or lengthen "
                f"hold_duration so the fault window survives")
        out = out[:len(out) - cut]
        truncated = True
    drops = _dropout_indices(time_axis.get("dropout_pattern"), len(out))
    for i in drops:
        out[i] = None
    return out, len(drops), truncated


def _series_labels(sig: dict, signature_index: int, variant: str,
                   fan_index: Optional[int] = None) -> dict:
    labels = dict(sig.get("labels") or {})
    # Injective series↔signature key（PR-2 歸因）：ALERTS 不帶 __name__，兩個
    # signature 若 topology labels 相同（或皆空）、僅 metric 名不同，讀回端無從
    # 判別——此 label 讓 series identity 對 signature 単射，同時解掉「同
    # metric+labels 兩簽章 import 互撞」。比照 waveform_variant 的合成期注入前例。
    labels["waveform_signature"] = str(signature_index)
    labels["waveform_variant"] = variant
    if fan_index is not None:
        labels["series"] = f"f{fan_index:02d}"  # index-derived, never uuid
    return labels


def synthesize_pack(pack: dict, seed: int = DEFAULT_SEED,
                    fanout: int = DEFAULT_FANOUT) -> list[Series]:
    """Synthesize every signature into its always-on variant set (R2-1).

    Non-boolean: base / noise / oscillation / fanout×N (+ companions).
    Boolean:     base / flapping / staleness_absence / fanout×N (+ companions).
    """
    pack_id = pack["pack"]["id"]
    out: list[Series] = []
    for si, sig in enumerate(pack["signatures"]):
        kind = sig["metric_kind"]
        wobble = float(sig.get("typical_wobble") or 0.0)
        noise_kind = sig.get("noise_kind", "gaussian")
        time_axis = sig.get("time_axis") or {}
        jitter_s = float(time_axis.get("jitter_s") or 0.0)
        base_values, fw = _base_waveform(sig)
        inherit = _inherit_expects(sig)

        variant_specs: list[tuple[str, list[float], str, list[str], Optional[int], bool]] = []
        # (variant, values, expects, auto_adjustments, fan_index, pre_truncated)

        if kind == "boolean":
            justification = ("boolean: typical_wobble noise exemption "
                             "(auto-justification: binary series has no analog noise floor); "
                             "flapping / staleness-absence variants generated instead")
            variant_specs.append(("base", base_values, inherit, [justification], None, False))
            variant_specs.append((
                "flapping", _apply_flapping(base_values, fw, sig), inherit,
                [justification], None, False))
            absence_expects = inherit if sig.get("agent_keeps_reporting") is False else "probe"
            absence_notes = [justification]
            if absence_expects == "probe":
                absence_notes.append(
                    "staleness-absence generated despite agent_keeps_reporting=true — "
                    "mechanical probe (down==absent blind-spot injection), not SME-declared")
            variant_specs.append((
                "staleness_absence", base_values[:fw[0]], absence_expects,
                absence_notes, None, True))
            for n in range(1, fanout + 1):
                variant_specs.append(("fanout", base_values, inherit, [justification], n, False))
        else:
            variant_specs.append(("base", base_values, inherit, [], None, False))
            rng_noise = random.Random(f"{seed}|{pack_id}|{si}|noise|0")
            variant_specs.append((
                "noise", _apply_noise(base_values, rng_noise, wobble, noise_kind),
                inherit, [], None, False))
            if sig["dips_back"]:
                osc_values, clamp_note = _apply_declared_dips(base_values, fw, sig)
                variant_specs.append((
                    "oscillation", osc_values, inherit,
                    [clamp_note] if clamp_note else [], None, False))
            else:
                variant_specs.append((
                    "oscillation", _apply_probe_dips(base_values, fw, sig), "probe",
                    ["mechanical full-depth oscillation probe generated despite "
                     "dips_back=false (P5: threshold-blind reset-trap coverage); "
                     "expects=probe — reported, never gates the verdict"], None, False))
            for n in range(1, fanout + 1):
                rng_fan = random.Random(f"{seed}|{pack_id}|{si}|fanout|{n}")
                variant_specs.append((
                    "fanout", _apply_noise(base_values, rng_fan, wobble, noise_kind),
                    inherit, [], n, False))

        for variant, values, expects, notes, fan_index, pre_trunc in variant_specs:
            notes = list(notes)
            if kind == "counter":
                values, clamped = _integrate_counter(values)
                if clamped:
                    notes.append(
                        f"counter: {clamped} negative instantaneous rate sample(s) "
                        f"clamped to 0 before integration (monotonicity)")
            elif kind == "gauge" and sig.get("min_value") is not None:
                min_v = float(sig["min_value"])
                clamped_n = sum(1 for v in values if v < min_v)
                if clamped_n:
                    values = [max(min_v, v) for v in values]
                    notes.append(
                        f"gauge: {clamped_n} sample(s) below min_value={min_v} "
                        f"clamped up (physical lower-bound guard)")
            samples, gaps, truncated = _apply_time_axis(values, time_axis)
            fault_window = _fault_window_s(variant, fw, len(samples), notes)
            hold_start_s = _hold_start_s(variant, fw, fault_window)
            series = Series(
                metric=sig["metric"],
                labels=_series_labels(sig, si, variant, fan_index),
                samples=samples,
                variant=variant,
                expects=expects,
                signature_index=si,
                fault_class=sig["fault_class"],
                source=sig["source"],
                jitter_s=jitter_s,
                truncated=truncated or pre_trunc,
                auto_adjustments=notes,
                rng_key=f"{seed}|{pack_id}|{si}|{variant}|{fan_index or 0}",
                fault_window=fault_window,
                hold_start_s=hold_start_s,
            )
            out.append(series)
            for ci, comp in enumerate(sig.get("companion_series") or []):
                comp_labels = dict(series.labels)
                comp_labels.update(comp.get("labels") or {})
                comp_level = float(comp["level"])
                comp_kind = comp.get("metric_kind", "gauge")
                comp_notes = [f"companion role={comp['role']} of {sig['metric']}"]
                if comp_kind == "counter":
                    # A constant counter renders rate()==0, so a ratio's
                    # denominator would divide by zero (+Inf when the
                    # numerator fires, NaN otherwise) — integrate to a
                    # cumulative ramp instead, mirroring _integrate_counter.
                    cum = 0.0
                    comp_samples: list[Optional[float]] = []
                    for s in samples:
                        if s is None:
                            comp_samples.append(None)
                            continue
                        cum += comp_level * STEP
                        comp_samples.append(round(cum, 6))
                    comp_notes.append(
                        "counter: companion level treated as a per-second rate, "
                        "integrated to a cumulative ramp (avoids rate()==0 → "
                        "ratio divide-by-zero)")
                else:
                    comp_samples = [None if s is None else comp_level for s in samples]
                out.append(Series(
                    metric=comp["metric"],
                    labels=comp_labels,
                    samples=comp_samples,
                    variant=variant,
                    expects="companion",
                    signature_index=si,
                    fault_class=sig["fault_class"],
                    source=sig["source"],
                    jitter_s=jitter_s,
                    truncated=series.truncated,
                    auto_adjustments=comp_notes,
                    rng_key=f"{seed}|{pack_id}|{si}|{variant}|{fan_index or 0}|comp{ci}",
                    fault_window=series.fault_window,  # companion 隨主 series（informational）
                    hold_start_s=series.hold_start_s,  # 隨主 series（informational）
                ))
    return out


# ── Materializations ─────────────────────────────────────────────────

def materialize_promtool(series_list: list[Series]) -> str:
    """Materialization (a): promtool fixture fragment (``values:`` notation,
    ``_`` == gap). Reference only — divergence-explanation input, NOT the
    catch-rate authority (that is (b)/VM).

    Cannot express jitter: promtool's ``values:`` notation has no per-sample
    timestamp, so a jittered series rendered here with its "clean" values
    would silently disagree with materialization (b) on fire/no-fire near an
    eval-window boundary — that divergence would then be misread as a
    MetricsQL engine difference instead of a data-fidelity artifact of this
    tool. So any series carrying jitter_s > 0 is masked to all-gap here
    (reference-only, no fabricated agreement); it can only be judged via
    materialization (b) / vmalert-replay."""
    lines = [
        "# waveform materialization (a) — promtool fixture fragment",
        "# role: Prometheus-behaviour REFERENCE only; catch-rate authority is materialization (b)/VM",
        f"# step: {STEP}s, origin: index 0 == T0 ({T0} epoch seconds)",
    ]
    lines.append(f"interval: {STEP}s")
    lines.append("input_series:")
    for s in series_list:
        lines.append(f"  - series: '{s.metric}{_fmt_labels(s.labels)}'")
        if s.jitter_s > 0:
            lines.append(
                f"    # ⚠️ jitter_s={_fmt(s.jitter_s)}：promtool values: 記法無 "
                "per-sample timestamp、結構上表達不了 jitter，此 series 全 gap"
                "（不可對帳）；只能在物化 (b)（vm import / vmalert-replay）判定")
            tokens = " ".join("_" for _ in s.samples)
        else:
            tokens = " ".join("_" if v is None else _fmt(v) for v in s.samples)
        lines.append(f"    values: '{tokens}'")
    return "\n".join(lines) + "\n"


def materialize_vm(series_list: list[Series]) -> str:
    """Materialization (b): Prometheus import text lines
    ``metric{labels} value ts_ms`` with absolute millisecond timestamps —
    the catch-rate authority. Jitter (when declared) applies here, drawn from
    the per-series seeded stream; gaps are simply absent lines."""
    lines = [
        "# waveform materialization (b) — Prometheus import lines (absolute ts, ms)",
        "# role: catch-rate authority (feeds VM / vmalert-replay)",
        f"# step: {STEP}s, T0: {T0} epoch seconds",
    ]
    for s in series_list:
        rng = random.Random(f"{s.rng_key}|jitter")
        label_str = _fmt_labels(s.labels)
        last_ts_ms = -1
        for idx, v in enumerate(s.samples):
            offset = rng.uniform(-s.jitter_s, s.jitter_s) if s.jitter_s > 0 else 0.0
            if v is None:
                continue
            ts_ms = int(round((T0 + idx * STEP + offset) * 1000))
            # Defense-in-depth monotonicity guard: schema caps jitter_s below
            # STEP/2 so jitter can never reorder samples, but a direct-lib caller
            # or a future STEP change must never emit out-of-order timestamps —
            # a non-monotone counter series reads as a phantom rate() reset and
            # silently corrupts the catch-rate authority.
            if ts_ms <= last_ts_ms:
                ts_ms = last_ts_ms + 1
            last_ts_ms = ts_ms
            lines.append(f"{s.metric}{label_str} {_fmt(v)} {ts_ms}")
    return "\n".join(lines) + "\n"


def build_metadata(pack: dict, series_list: list[Series], seed: int, fanout: int) -> dict:
    """Per-output provenance metadata: variant kind, expects inheritance (or
    probe/companion), seed, step, auto-modulation trail (D8.4 no-silent-caps)."""
    any_jitter = any(s.jitter_s > 0 for s in series_list)
    return {
        "pack_id": pack["pack"]["id"],
        "domain": pack["pack"]["domain"],
        "seed": seed,
        "fanout": fanout,
        "step_seconds": STEP,
        "t0_epoch_seconds": T0,
        "readback_signed_off": bool(pack["pack"].get("readback_signed_off")),
        "independent_of_rule_conversion": bool(
            pack["pack"].get("independent_of_rule_conversion")),
        "materializations": {
            "promtool_fixture": {
                "role": "reference-only",
                "includes_jitter": False,
                "jitter_declared_but_not_representable": any_jitter,
            },
            "vm_import": {
                "role": "catch-rate-authority",
                "includes_jitter": any_jitter,
            },
        },
        "series": [
            {
                "metric": s.metric,
                "labels": dict(sorted(s.labels.items())),
                "variant": s.variant,
                "expects": s.expects,
                "signature_index": s.signature_index,
                "fault_class": s.fault_class,
                "source": s.source,
                "sample_count": len(s.samples),
                "gap_count": sum(1 for v in s.samples if v is None),
                "truncated": s.truncated,
                "jitter_s": s.jitter_s,
                # 可偵測故障窗（秒、相對窗起點；PR-3 temporal-match 血緣——語義見
                # _fault_window_s docstring）：[start, end] / [start, null]（absence
                # 開放至窗尾）/ null（截斷吃光、不可定義）。
                "fault_window_s": list(s.fault_window) if s.fault_window is not None else None,
                # fault-hold 段起點（秒；PR-3 early-onset 過敏標記血緣——語義見
                # _hold_start_s docstring）。absence = 窗下界；截斷吃光 = null。additive 欄。
                "hold_start_s": s.hold_start_s,
                "auto_adjustments": list(s.auto_adjustments),
            }
            for s in series_list
        ],
    }


# ── Readback rendering (SME sign-off) ────────────────────────────────

def _sparkline(values: list[float], width: int = 64) -> str:
    if not values:
        return ""
    if len(values) > width:
        picked = [values[round(i * (len(values) - 1) / (width - 1))] for i in range(width)]
    else:
        picked = list(values)
    lo, hi = min(picked), max(picked)
    if hi == lo:
        return _SPARK_BLOCKS[3] * len(picked)
    return "".join(
        _SPARK_BLOCKS[int((v - lo) / (hi - lo) * (len(_SPARK_BLOCKS) - 1) + 0.5)]
        for v in picked)


def render_readback(pack: dict) -> str:
    """ASCII sparkline + ZH summary per signature, for SME read-back sign-off
    (P1 (c+)). Renders the CLEAN base envelope — the thing the SME described —
    not the noise/probe variants (those are compiler territory)."""
    p = pack["pack"]
    lines = [
        f"── waveform pack 回讀（readback）: {p['id']} ──",
        f"domain: {p['domain']} / author_role: {p['author_role']}",
        "",
    ]
    for si, sig in enumerate(pack["signatures"]):
        base_values, _fw = _base_waveform(sig)
        dips = sig.get("dips_back")
        dip_txt = "會"
        if dips:
            dd = sig["dip_detail"]
            dip_txt = f"會（深度 {_fmt(float(dd['depth']))}、約每 {dd['period']} 一次）"
        else:
            dip_txt = "不會"
        agent_txt = "仍持續回報" if sig.get("agent_keeps_reporting") else "會停止回報（資料中斷）"
        wake_txt = "是（must_detect）" if sig.get("must_detect") else "否"
        wobble = sig.get("typical_wobble")
        wobble_txt = ("（boolean 指標，無底噪欄）" if sig["metric_kind"] == "boolean"
                      else f"±{_fmt(float(wobble))}（原生單位）")
        lines.extend([
            f"[signature {si + 1}] {sig['description']}",
            f"  指標: {sig['metric']}（{sig['metric_kind']} / {sig['shape_class']} / 來源 {sig['source']}）",
            f"  正常水位 {_fmt(float(sig['normal_level']))} → 故障水位 {_fmt(float(sig['fault_level']))}（原生單位）",
            f"  惡化歷時 {sig['onset_duration']}，故障持續 {sig['hold_duration']}",
            f"  平常波動: {wobble_txt}",
            f"  故障期間短暫掉回正常: {dip_txt}",
            f"  故障時監控代理: {agent_txt}",
            f"  半夜想被叫醒: {wake_txt}",
            "  形狀（乾淨包絡，時間 →）:",
            f"    {_sparkline(base_values)}",
            "",
        ])
    lines.extend([
        "請 SME 確認以上形狀與描述皆與實際故障相符。",
        "確認無誤後，由平台工程師在 pack 標記 readback_signed_off: true（簽核紀錄）。",
        "簽核前 SME 不得閱讀 VM 規則與閾值（盲寫治理，ADR-030 D2）。",
    ])
    return "\n".join(lines) + "\n"
