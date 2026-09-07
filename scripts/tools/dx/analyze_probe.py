#!/usr/bin/env python3
"""Summarise BenchmarkProbeWriteLatency PROBE records - write vs load attribution.

⛔ It summarises; it does not measure. Both modes answer "what do these records
say", never "what would a run today produce".

Two output modes, ONE computation
---------------------------------
    --from-log PATH      the per-job CI summary (Markdown, one probe.out).
                         Called by .github/workflows/bench-probe-write-latency.yaml.
    --archive DIR        the archive report (plain text, probe-run{1,2,3}.txt
                         in DIR — all three required, see main()),
                         which additionally puts separate dispatches side by side.

⭐ Why one file with two renderers, rather than one report: the two consumers
need different things. The CI summary answers "what happened in THIS job" and is
read in a GitHub Job Summary pane; the archive report answers "do the numbers in
that directory's README follow from the collected data" and puts three dispatches
on one axis. What they share is every decision in between - which rows are
measurement rows, how records are matched, what counts as a rejectable input, and
every statistic. That shared middle is what used to exist twice.

History (TRK-373 / #1731)
-------------------------
Until this file existed, the computation lived twice: inline in the workflow's
Summarize step, and in docs/internal/audit-reports/bench-probe-2026-09/
analyze_probe.py. Nothing checked that the two agreed, and six behavioural
divergences had accumulated. Each is resolved here in one direction, and the
resolution is annotated at the site rather than only in the commit message:

    1. ambiguous calibration round -> the workflow's behaviour (say so explicitly)
    2. record matching            -> the workflow's behaviour (word-boundary)
    3. minimum rounds for corr    -> the workflow's behaviour (MIN_ROUNDS_FOR_CORR)
    4. NaN rendering              -> UNCHANGED on both sides, deliberately
    5. duplicate PROBEENV records -> the workflow's behaviour (do not de-duplicate)

⛔ #4 is deliberately NOT unified: each renderer keeps the format string it had
("+nan" here, "nan" there). Unifying report conventions is a separate concern
from removing the duplicate. It stays open under TRK-375 (#1733).

⚠️ The count is over what was enumerated, not a claim that the copies had no
other difference. The replaced copy is gone, so completeness cannot be
established now. The enumeration itself is recorded in dev/trk-373/ROUNDS.jsonl.
"""

import argparse
import contextlib
import io
import functools
import os
import pathlib
import re
import statistics as st
import sys

# ⛔ Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/. The CI
# renderer prints CJK and emoji, so an un-hardened stdout makes even `--help`
# crash on a legacy Windows console (cp950). Same two-insert idiom as the sibling
# analyze_bench_history.py: parent for the repo layout, self-dir for the Docker
# flat layout. ⚠️ This tool is NOT bundled into the da-tools image (it is absent
# from build.sh's TOOL_FILES allowlist), so only the parent insert is functionally
# required; the self-dir insert is kept for parity.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402

# #1497's three same-day dispatches read M/W = +30.38% / +4.85% / +3.98%. The
# swing to be explained is the gap between the modes, in percentage points.
TARGET_SWING_PP = 30.38 - 3.98

# ⛔ A correlation over two or three points is a coin flip that reads as a
# conclusion. Below this many rounds the report says "not enough rounds" instead
# - "could not measure" and "measured, nothing there" have to be different lines.
# ⚠️ Divergence 3: only the workflow copy had this. The archive copy printed
# 1.000 for n=2, which is what Pearson always returns there.
MIN_ROUNDS_FOR_CORR = 5

# ⛔ The archive report's file list is FIXED, exactly as the script this replaced
# had it. It is not a glob — see the comment at the call site in main() for the
# three ways globbing changed behaviour, and note that the CROSS DISPATCH section
# hard-codes the word "three" in its prose.
RUNS = ["probe-run1.txt", "probe-run2.txt", "probe-run3.txt"]

# ⛔ Divergence 2: records are NOT anchored at line start. `go test` prefixes the
# first output line of each invocation with `BenchmarkProbeWriteLatency-4   \t`,
# and `-benchtime=Nx` always produces two invocations (a b.N==1 calibration pass
# and the measurement pass), so in practice the second PROBEENV is pushed off the
# line start. The archive copy used `^PROBEROW` + .match() and silently dropped
# such rows; worse, if the swallowed row was the calibration round, the filter
# below stopped firing and cold-start got pooled into the measurements.
REC = re.compile(r"\bPROBE(ROW|ENV) (.*)$")
TAIL = re.compile(r"\bPROBETAIL round=(\d+) rank=(\d+) iter=(\d+) w=(\d+) l=(\d+)\s*$")

# ⛔ The fields the renderers index with [] — i.e. exactly those whose absence
# raises KeyError part-way through a report. NOT the set the producer emits:
# `write_p50`, `write_p90` and `load_p90` are emitted and never read, and
# rejecting a record for missing one of those would refuse input the report can
# actually summarise. ⚠️ This is a hand-written list of a mechanical fact, so it
# can drift the moment someone indexes a new field. `test_required_row_fields_
# match_what_the_code_indexes` re-derives it from this file's own AST and pins
# the two together; do not edit this tuple without letting that test agree.
REQUIRED_ROW_FIELDS = ("iters", "load_max", "load_p50", "load_p99", "load_sum",
                       "round", "write_max", "write_p99", "write_sum")


def parse_row(payload):
    """`(fields, None)` for a usable PROBEROW payload, `(fields, reason)` if not.

    ⛔ `fields` is returned EVEN WHEN the record is defective, carrying whatever
    parsed. The caller needs it: a record whose `bench_n` says it is the
    calibration round is dropped before any renderer reads it, so refusing the
    whole log over a field missing THERE would reject input that summarises
    fine — the same over-strictness REQUIRED_ROW_FIELDS is narrow to avoid.
    Found by blind review of the first draft, which refused it.

    ⛔ Every defect here used to escape `load()` as a bare exception — a
    ValueError from `dict(kv.split("="))`, a ValueError from `int()`, or a
    KeyError raised much later, mid-report, by a renderer. See TRK-375 (#1733).
    """
    fields, why = {}, None
    for kv in payload.split():
        if "=" not in kv:
            why = why or f"field {kv!r} has no '='"
            continue
        k, v = kv.split("=", 1)
        try:
            fields[k] = int(v)
        except ValueError:
            why = why or f"field {k}={v!r} is not an integer"
    if why is None:
        missing = [f for f in REQUIRED_ROW_FIELDS if f not in fields]
        if missing:
            why = "missing " + ", ".join(missing)
    return fields, why


class Session:
    """One parsed probe log: the rows, what produced them, and how it was read."""

    def __init__(self, name, rows, env, ncal, calib_ambiguous, tails,
                 malformed=(), discarded=()):
        self.name = name
        self.rows = rows
        self.env = env
        self.ncal = ncal
        self.calib_ambiguous = calib_ambiguous
        self.tails = tails
        # ⛔ Carried, not raised. `reject()` is the ONLY place allowed to refuse;
        # a defect that escapes `load()` as an exception bypasses it entirely.
        self.malformed = list(malformed)
        # ⛔ Defective records the calibration exemption let through. They are
        # NOT a refusal, but they ARE data that went missing, so both renderers
        # say so: a record thrown away without a word is exactly the
        # "could not measure" wearing "measured" costume this tool exists to
        # prevent. Found by blind review of the exemption, which discarded them
        # in silence — the report was byte-identical to a clean run except for
        # the calibration-dropped clause quietly going away.
        self.discarded = list(discarded)


def load(text, name):
    """Parse one probe log into a Session. No validation here - see reject().

    ⛔ "No validation" means no JUDGEMENT — it never meant "assume well-formed
    input". Malformed PROBEROW records used to leave this function as bare
    exceptions, so `reject()` — the one place allowed to refuse — never ran and
    the caller got a traceback instead of an ::error::. They are now collected
    and carried; `reject()` decides. TRK-375 (#1733).
    """
    rows, env, tails, defective = [], [], {}, []
    for lineno, line in enumerate(text.splitlines(), 1):
        m = REC.search(line)
        if m:
            if m.group(1) == "ENV":
                # ⛔ Divergence 5: NOT de-duplicated. The archive copy applied
                # dict.fromkeys here, which is a no-op on real data (the two
                # records differ in b.N) but hides a genuinely repeated record -
                # and a repeated PROBEENV means two invocations of identical
                # shape, which `-benchtime=Nx` does not produce. In a section
                # whose whole job is to identify what was measured, swallowing
                # that is the wrong default.
                env.append(m.group(2).strip())
            else:
                row, why = parse_row(m.group(2))
                if why is not None:
                    defective.append((lineno, why, row))
                else:
                    rows.append(row)
            continue
        m = TAIL.search(line)
        if m:
            # ⛔ DEAD as of writing: nothing below reads `tails`. Every tail
            # number in both reports comes from PROBEROW's own fields. Carried
            # over unchanged rather than dropped, so this change stays a pure
            # de-duplication; removing it is TRK-375 (#1733), which owns it.
            tails.setdefault(int(m.group(1)), []).append(
                (int(m.group(4)), int(m.group(5))))

    # ⛔ Drop the calibration round. Under `-benchtime=Nx` Go still runs the body
    # once at b.N==1 to calibrate, and that pass is the first after process start
    # (cold caches, first-touch faults). Pooling it into a spread reports start-up
    # as measurement noise - the same cold-start effect was measured at ~+24.8%
    # in audit-reports/bench-aa-2026-08/README.md §三.
    # ⚠️ Only when rows with b.N>1 actually exist: under a manual `-benchtime=1x`
    # every row is a calibration row, and dropping all of them would delete the
    # data and still print a report.
    calib = [r for r in rows if r.get("bench_n") == 1]
    calib_ambiguous = False
    # ⛔ Derived from the MEASUREMENT rows, not from `calib`: whether the filter
    # below fires decides whether a defective calibration record matters, and a
    # record too broken to parse never reaches `calib`. Keying off `calib` made
    # the predicate answer "no calibration row here" for exactly the input it
    # exists to classify — measured: the first draft of this fix changed nothing.
    dropped_calibration = any(r.get("bench_n", 1) > 1 for r in rows)
    if calib and dropped_calibration:
        rows = [r for r in rows if r.get("bench_n", 1) > 1]
    elif calib:
        # ⛔ Divergence 1: say the ambiguity out loud. The archive copy computed
        # ncal unconditionally and printed it even when this branch applied, so
        # it announced "3 measurement rounds, 3 calibration dropped" over 3 rows
        # - a header that contradicts itself.
        calib_ambiguous = True
    # ⛔ A defect in a record the calibration filter just DROPPED is not a defect
    # in anything the report reads, and refusing over it rejects input that
    # summarises fine. Measured on the first draft of this change: a calibration
    # row missing `load_p50` turned a clean rc=0 report into rc=2. Found by blind
    # review; it is the same over-strictness REQUIRED_ROW_FIELDS is narrow to
    # avoid, one level up — per-RECORD instead of per-FIELD.
    # ⚠️ Only when the filter actually fired, and only for a record whose
    # `bench_n` itself parsed as 1. A record too broken to classify stays
    # reported: loud beats dropping it on a guess.
    # ⛔ ONE predicate, partitioned in one pass. Written as two comprehensions
    # with the condition duplicated, the two lists can disagree — and when they
    # do, the disagreement is UNOBSERVABLE: a record in both `malformed` and
    # `exempt` is refused before anything renders `discarded`, so no test can
    # see it. Measured while dogfooding: mutating one copy of the condition left
    # the whole suite green. A predicate that cannot drift beats a test for a
    # state nothing can reach.
    exempt, malformed = [], []
    for lineno_, why_, partial_ in defective:
        bucket = (exempt if dropped_calibration and partial_.get("bench_n") == 1
                  else malformed)
        bucket.append((lineno_, why_))
    return Session(name, rows, list(env), len(calib), calib_ambiguous,
                   tails, malformed, exempt)


def reject(session, thin_msg=None):
    """Return an ::error:: string if this session must not be summarised.

    ⛔ `thin_msg` exists because the two originals worded the "too few rows"
    refusal DIFFERENTLY, and that wording is part of each report's output.
    ⚠️ The other two refusals (no PROBEENV, iters<=0) are NOT parameterised —
    they were already worded identically in both originals.
    """
    # ⛔ FIRST. A malformed record is not a thin log: reporting "parsed 1 row"
    # for input whose rows failed to parse names the symptom and hides the cause.
    if session.malformed:
        shown = "; ".join(f"line {n}: {why}" for n, why in session.malformed[:3])
        more = (f" (+{len(session.malformed) - 3} more)"
                if len(session.malformed) > 3 else "")
        return (f"::error::{len(session.malformed)} malformed PROBEROW"
                f" record(s) — {shown}{more}")
    if len(session.rows) < 2:
        if thin_msg is not None:
            return thin_msg(session)
        return (f"::error::expected >=2 measurement PROBEROW records, parsed"
                f" {len(session.rows)} (calibration rows seen: {session.ncal})")
    if not session.env:
        return "::error::no PROBEENV record — cannot identify what was measured"
    # ⛔ `iters` is a divisor further down. Zero here is a MALFORMED record, not a
    # degenerate-but-valid measurement, so it is rejected rather than printed n/a.
    bad = [r["round"] for r in session.rows if r.get("iters", 0) <= 0]
    if bad:
        return (f"::error::rounds {bad} report iters<=0; a round with no"
                " iterations cannot be summarised")
    # ⛔ The per-iteration figures divide by ONE `iters` taken from the first row,
    # while the level/above-level split divides row by row. On a log whose rows
    # disagree — two differently-configured runs concatenated, which the
    # transcribe-by-hand archive flow can produce — that mixture is silently
    # wrong: measured, the reported per-iteration write average halves when only
    # the first row's `iters` is doubled, rc=0, no warning. TRK-375 (#1733).
    spread = sorted({r["iters"] for r in session.rows})
    if len(spread) > 1:
        return (f"::error::rounds report differing iters {spread}; the"
                " per-iteration figures divide by a single value, so a log"
                " mixing them cannot be summarised")
    return None


def corr(x, y):
    mx, my = st.mean(x), st.mean(y)
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    den = (sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y)) ** 0.5
    return num / den if den else float("nan")


def ms(x):
    return x / 1e6


def pct(num, den, spec=".1f", na="n/a (denominator is 0)"):
    """`100*num/den` as a percent string, or an explicit n/a when den is zero.

    ⛔ Every divisor in these reports is a MEASURED quantity, so every one of them
    can be zero on degenerate input. An unguarded division does not degrade the
    report - it kills it, and a report that prints nothing cannot tell "could not
    measure" apart from "measured, nothing there". That distinction is the only
    reason this tool exists, so it is not allowed to die on the way to stating it.

    ⚠️ This is not a claim that any real probe run produces a zero denominator.
    None of the degenerate states here were produced by a run; they were reached
    by constructing the input. Unreachability is NOT claimed - it was never
    enumerated. The guard is justified by the failure mode, not by its odds.
    """
    return f"{100 * num / den:{spec}}%" if den else na


# ── level / above-p50 split ──────────────────────────────────────────────────
# ⛔ `load_sum` can grow two ways and they mean opposite things: every iteration
# gets slower (a level shift), or a few iterations stall (an episode). Mechanism 1
# predicts an episode, but on the SUM the two look identical. Splitting at the
# round's own p50 separates them: `p50 x iters` is the level, the remainder is
# everything above it.
# ⚠️ The discriminator is which half CORRELATES with the round total, not which
# half is bigger - a whole-distribution shift moves both.
def level(r):
    return r["load_p50"] * r["iters"]


def above(r):
    return r["load_sum"] - r["load_p50"] * r["iters"]


def total(r):
    return r["write_sum"] + r["load_sum"]


# ── renderer: per-job CI summary (Markdown) ──────────────────────────────────

def render_ci(session):
    _pct = functools.partial(pct, na="n/a（除數為 0）")
    rows = session.rows
    if session.calib_ambiguous:
        dropped = f"⚠️ 全部 {session.ncal} 列都是 b.N==1，無法區分校準輪，全數保留"
    elif session.ncal:
        dropped = f"（已排除 {session.ncal} 個 b.N==1 的校準輪）"
    else:
        dropped = ""

    print("## Probe: write vs load latency (#1497 mechanism 1)\n")
    print("```")
    for e in session.env:
        print(f"PROBEENV {e}")
    print(f"rounds parsed: {len(rows)} {dropped}".rstrip())
    print("```\n")
    # ⛔ OUTSIDE the fence. Printed between the ``` markers this renders as
    # literal text in a code block instead of the callout it is written as —
    # verified against a Markdown renderer, not by reading. A note whose entire
    # job is "being thrown away must be SEEN, not just present in the bytes"
    # being swallowed by the raw-log styling is the same failure it exists to
    # prevent, one layer up. Found by blind review. The `> ⛔ 判讀限制` note
    # further down is outside a fence for the same reason.
    if session.discarded:
        where = ", ".join(f"line {n}" for n, _ in session.discarded)
        print(f"> ⚠️ {len(session.discarded)} 筆校準輪記錄格式有誤、無法解析，已略過"
              f"（{where}）。⛔ 它們本來就會被校準過濾器丟棄，所以量測結果不受影響——"
              f"但「被丟掉」這件事必須說出來，不能只是消失。\n")

    tot = [total(r) for r in rows]
    w = [r["write_sum"] for r in rows]
    l = [r["load_sum"] for r in rows]
    med = st.median(tot)

    print("| quantity | value |")
    print("|---|---|")
    print(f"| round total, median | {med/1e6:.1f} ms |")
    print(f"| round total, spread (max-min)/median | **{_pct(max(tot)-min(tot), med)}** |")
    print(f"| write share of all time | **{_pct(sum(w), sum(tot), '.3f')}** |")
    print(f"| write_sum spread | {_pct(max(w)-min(w), st.median(w))} |")
    print(f"| load_sum spread | {_pct(max(l)-min(l), st.median(l))} |")

    # 歸因只在散佈真的出現時才有意義。門檻寫死在這裡是刻意的：它不是
    # 判定器，只是決定要印哪一段字，好讓「沒抓到」與「抓到且乾淨」不會
    # 長成同一句話。
    worst = max(rows, key=total)
    base = st.median(tot)
    excess = total(worst) - base
    print(f"\n最慢的一輪：round={worst['round']}，比中位多 "
          f"{excess/1e6:.1f} ms（{_pct(excess, base, '+.1f')}）。")
    if excess > 0:
        dw = worst["write_sum"] - st.median(w)
        dl = worst["load_sum"] - st.median(l)
        print(f"其中寫入貢獻 {dw/1e6:+.1f} ms、載入貢獻 {dl/1e6:+.1f} ms "
              f"⇒ 超額有 **{_pct(dw, excess)}** 落在寫入路徑。")
    print(f"\n該輪的寫入尾端：p99={worst['write_p99']/1000:.1f} µs、"
          f"max={worst['write_max']/1000:.1f} µs；"
          f"載入 p99={worst['load_p99']/1e6:.2f} ms、max={worst['load_max']/1e6:.2f} ms。")

    print("\n### 這批 round-to-round 變異是什麼形狀\n")
    if len(rows) < MIN_ROUNDS_FOR_CORR:
        print(f"⚠️ 只有 {len(rows)} 輪，少於 {MIN_ROUNDS_FOR_CORR} 輪，"
              "**不印相關係數**——這是輪數不夠，不是形狀不明顯。")
    else:
        lvl = [level(r) for r in rows]
        abv = [above(r) for r in rows]
        sd_t = st.stdev(tot)
        print("| 分量 | corr(round total, ·) | 自身 sd | 自身 sd ÷ round total sd |")
        print("|---|---|---|---|")
        for nm, v in (("水位 `p50 × iters`", lvl), ("水位以上的質量", abv),
                      ("`write_sum`", w)):
            # ⛔ Divergence 4 stays as it was: "+.3f" here renders a NaN
            # correlation as "+nan", the archive renderer's ".3f" renders it as
            # "nan". Neither uses this report's own n/a convention. Unifying that
            # is TRK-375 (#1733), not this change.
            print(f"| {nm} | {corr(tot, v):+.3f} | {st.stdev(v)/1e6:.2f} ms |"
                  f" {_pct(st.stdev(v), sd_t)} |")
        print("\n⚠️ 相關係數說「哪一個跟著動」，最後一欄說「它最多能推動多少」。"
              "一個分量要當成因，兩欄都要成立——這是第二欄存在的**設計理由**。"
              "⛔ 它目前**沒有實測背書**：原本佐證它的那組合成資料數字，因為"
              "產生它的 harness 從未進 repo、無人能核對，已一併刪除（TRK-374 補回）。"
              "⇒ 這張表要兩欄一起讀，但不要把「兩欄一起讀就分得出形狀」當成已驗證。")
        print("\n⛔ 最後一欄**不是** variance 分解，三列不會加總到 100%："
              "三個分量彼此相關，>100% 表示它被另一個分量抵銷掉一部分"
              "（水位平移時水位與『水位以上的質量』反向）。")

    # 機制 1 的量級門檻。⭐ 這一段刻意**不依賴有沒有抓到 episode**：它問的是
    # 「要產生 #1497 那個量級，寫入路徑得做到什麼」，然後拿它跟實際看到的最大
    # 一次相比。一次什麼都沒抓到的執行也答得出這一題。
    # ⛔ 它給的是對**已觀測** episode 的上界，不是「不可能更大」的證明。
    need = TARGET_SWING_PP / 100 * med
    biggest = max(w) - st.median(w)
    iters_per_round = rows[0]["iters"]
    med_w = st.median(w)
    print("\n### 機制 1 要達到 #1497 的量級得做到什麼\n")
    # ⛔ 兩個分支各自寫成完整句子，不共用前半段。共用前半段時 null 情況會印成
    # 「高於中位最多的一輪是 0.0 ms，沒有任何一輪高於中位」——前半句斷言那一輪
    # 存在，後半句說它不存在，自相矛盾。
    print(f"單側擺 {TARGET_SWING_PP:.2f}% 在 {med/1e6:.0f} ms 的一輪上是 **{need/1e6:.0f} ms**。"
          + (f"本次 {len(rows)} 輪裡，`write_sum` 高於中位最多的一輪是 "
             f"**{biggest/1e6:.1f} ms**（差 **{need/biggest:.0f}×**）。" if biggest > 0 else
             f"本次 {len(rows)} 輪裡**沒有任何一輪的 `write_sum` 高於中位**，"
             "所以沒有正向超額可以拿來比——這是一個 null 觀測，不是量測失敗。"))
    print(f"換算到每次迭代：中位輪的寫入平均 {med_w/iters_per_round/1000:.1f} µs，"
          f"要達標得在全部 {iters_per_round} 次迭代上平均 {need/iters_per_round/1e6:.2f} ms"
          + (f"，即整個寫入路徑成本的 **{need/med_w:.0f}×**、而且是**持續**不是尖峰。"
             if med_w > 0 else
             "。⚠️ 中位輪的寫入半邊是 0 ns，因此它的倍數沒有定義。"))

    print("\n> ⛔ 判讀限制：本 job 只跑一台 runner、一個 job，跨輪散佈與 #1497 "
          "觀察到的**跨 dispatch** 散佈不是同一個量。若上面的 spread 沒有重現 "
          "#1497 的量級，那是**這次沒抓到**，不是「沒有」。")
    return 0


# ── renderer: archive report (plain text, cross-dispatch) ────────────────────

def render_archive(sessions):
    print("=" * 74)
    print("IDENTITY OF THE THING MEASURED")
    print("=" * 74)
    for s in sessions:
        print(f"  {s.name}")
        for e in s.env:
            print(f"      PROBEENV {e}")
    envs = {e for s in sessions for e in s.env}
    # Strip the b.N field, which legitimately differs between the calibration
    # invocation and the measurement one.
    shapes = {re.sub(r" b\.N=\d+", "", e) for e in envs}
    print(f"\n  distinct (goos, goarch, numcpu, go, iters) tuples across all runs: {len(shapes)}")
    for sh in sorted(shapes):
        print(f"      {sh}")
    if len(shapes) != 1:
        print("  ⚠️ the runs are NOT all the same shape — do not pool them below")

    print()
    print("=" * 74)
    print("PER DISPATCH")
    print("=" * 74)
    for s in sessions:
        rows = s.rows
        tot = [total(r) for r in rows]
        w = [r["write_sum"] for r in rows]
        med = st.median(tot)
        worst = max(rows, key=total)
        excess = total(worst) - med
        dw = worst["write_sum"] - st.median(w)
        # ⛔ Divergence 1: an ambiguous calibration round is now stated, not
        # reported as a count that contradicts the round count beside it.
        if s.calib_ambiguous:
            how = (f"{len(rows)} measurement rounds, ⚠️ all {s.ncal} rows are"
                   " b.N==1 — calibration indistinguishable, none dropped")
        else:
            how = f"{len(rows)} measurement rounds, {s.ncal} calibration dropped"
        if s.discarded:
            # ⛔ Same shape as the CI renderer's line list, deliberately: the
            # earlier version inflected "line"/"lines" on the count, and that
            # branch was pure grammar with no fixture behind it — a dead branch
            # kept alive only by a plural. Repeating "line N" needs no inflection
            # and so cannot be wrong.
            where = ", ".join(f"line {n}" for n, _ in s.discarded)
            how += (f", {len(s.discarded)} unparseable calibration record(s)"
                    f" skipped ({where})")
        print(f"\n  {s.name}  ({how})")
        print(f"      round total   median {ms(med):8.1f} ms   min {ms(min(tot)):8.1f}   "
              f"max {ms(max(tot)):8.1f}   spread {pct(max(tot) - min(tot), med, '6.2f'):>7}")
        print(f"      write_sum     median {ms(st.median(w)):8.3f} ms  min {ms(min(w)):8.3f}   "
              f"max {ms(max(w)):8.3f}   share of all time {pct(sum(w), sum(tot), '.3f')}")
        print(f"      write tail    worst p99 over rounds {max(r['write_p99'] for r in rows) / 1000:8.1f} us   "
              f"worst single write {max(r['write_max'] for r in rows) / 1000:8.1f} us")
        line = (f"      slowest round #{worst['round']}: {ms(excess):+.1f} ms vs median round "
                f"({pct(excess, med, '+.2f')})")
        # ⛔ The attribution clause is omitted, not filled with n/a, when there is
        # no excess to attribute: attribution only means anything once a spread
        # actually appeared, so "did not catch one" and "caught one and it was
        # clean" must not come out as the same sentence. Printing "= n/a of that
        # excess" instead implies an attribution exists and is merely unavailable.
        if excess > 0:
            line += (f"; the write half contributes {ms(dw):+.3f} ms "
                     f"= {pct(dw, excess)} of that excess")
        print(line)

    print()
    print("=" * 74)
    print("CROSS DISPATCH — the axis #1497 observed its bimodality on")
    print("=" * 74)
    meds = [st.median([total(r) for r in s.rows]) for s in sessions]
    gm = st.median(meds)
    for s, m in zip(sessions, meds):
        print(f"  {s.name:<18} median round total {ms(m):8.1f} ms   "
              f"{pct(m - gm, gm, '+6.2f'):>7} vs the middle dispatch")
    print(f"  spread of the three medians: {pct(max(meds) - min(meds), gm, '.2f')}")
    print(f"  ⛔ #1497's gap between modes, for comparison: {TARGET_SWING_PP:.2f} pp")

    allrows = [r for s in sessions for r in s.rows]
    tot = [total(r) for r in allrows]
    base = [level(r) for r in allrows]
    abv = [above(r) for r in allrows]
    w = [r["write_sum"] for r in allrows]

    print()
    print("=" * 74)
    print("SHAPE OF THE ROUND-TO-ROUND VARIATION — level shift or episode?")
    print("=" * 74)
    # ⛔ Divergence 3: the round-count floor now applies here too. The archive
    # copy had no floor and printed 1.000 over two points, which is what Pearson
    # returns there regardless of the data.
    thin = [s for s in sessions if len(s.rows) < MIN_ROUNDS_FOR_CORR]
    if thin:
        print(f"⚠️ {', '.join(s.name for s in thin)}: fewer than {MIN_ROUNDS_FOR_CORR}"
              " rounds — correlations NOT printed. That is too few rounds, not a"
              " shape that failed to show.")
    else:
        print(f"{'dispatch':<18}{'n':>4}{'corr(total, p50xN)':>21}{'corr(total, above-p50)':>24}"
              f"{'corr(total, write_sum)':>24}")
        for s in sessions:
            rs = s.rows
            t = [total(r) for r in rs]
            b = [level(r) for r in rs]
            a = [above(r) for r in rs]
            ww = [r["write_sum"] for r in rs]
            print(f"{s.name:<18}{len(rs):>4}{corr(t, b):>21.3f}{corr(t, a):>24.3f}{corr(t, ww):>24.3f}")
        print(f"{'POOLED':<18}{len(allrows):>4}{corr(tot, base):>21.3f}{corr(tot, abv):>24.3f}"
              f"{corr(tot, w):>24.3f}")

    print(f"\n  standard deviation over the pooled {len(allrows)} rounds:")
    print(f"      round total           {ms(st.stdev(tot)):8.2f} ms   (range {ms(max(tot) - min(tot)):7.2f} ms)")
    print(f"      level  (p50 x iters)  {ms(st.stdev(base)):8.2f} ms   (range {ms(max(base) - min(base)):7.2f} ms)"
          f"  = {pct(st.stdev(base), st.stdev(tot), '5.1f'):>6} of the round total's sd")
    print(f"      above-p50 mass        {ms(st.stdev(abv)):8.2f} ms   (range {ms(max(abv) - min(abv)):7.2f} ms)"
          f"  = {pct(st.stdev(abv), st.stdev(tot), '5.1f'):>6}")
    print(f"      write_sum             {ms(st.stdev(w)):8.2f} ms   (range {ms(max(w) - min(w)):7.2f} ms)"
          f"  = {pct(st.stdev(w), st.stdev(tot), '5.1f'):>6}")
    print("\n  ⚠️ correlation says which quantity moves WITH the round; the sd column says"
          "\n     how much it could move the round at all. A term needs both to be a cause.")

    print()
    print("=" * 74)
    print("WHAT MECHANISM 1 WOULD HAVE TO DO")
    print("=" * 74)
    med_round = st.median(tot)
    need = TARGET_SWING_PP / 100 * med_round
    biggest = max(w) - st.median(w)
    med_w = st.median(w)
    iters = allrows[0]["iters"]
    print(f"  A one-sided swing of {TARGET_SWING_PP:.2f}% on a {ms(med_round):.0f} ms round is "
          f"{ms(need):.0f} ms.")
    # ⛔ The two branches are each a whole sentence; they do NOT share a preamble.
    # Sharing one printed "largest excursion above the median: 0.0 ms" and then
    # "no round sat above the median" - the first half asserts that round exists,
    # the second says it does not.
    if biggest > 0:
        print(f"  Largest write-path excursion above the median write_sum, in all "
              f"{len(allrows)} rounds: {ms(biggest):.1f} ms => short by a factor of "
              f"{need / biggest:.0f}x")
    else:
        print(f"  In all {len(allrows)} rounds no write_sum sat above the median, so there"
              " is no positive excursion to compare against (a null observation, not a"
              " measurement failure)")
    print(f"  Per iteration: the median round's write half averages "
          f"{med_w / iters / 1000:.1f} us;")
    print(f"     to reach {ms(need):.0f} ms it would have to average "
          f"{need / iters / 1e6:.2f} ms across all {iters} iterations,")
    if med_w > 0:
        print(f"     i.e. {need / med_w:.0f}x its entire observed cost, sustained — not in a spike.")
    else:
        print("     the median round's write half is 0 ns, so no multiple of it is defined.")
    print("\n  ⛔ This bounds the episodes that WERE seen. It is not a proof that no larger"
          "\n     episode exists: the high mode itself was not reproduced in these runs"
          "\n     (see the cross-dispatch spread above).")
    return 0


def emit(render):
    """Run a renderer with stdout buffered, so a crash prints NOTHING.

    ⛔ The renderers stream dozens of print() calls straight to stdout, and the
    workflow tees stdout into $GITHUB_STEP_SUMMARY *as it arrives*. A crash
    part-way therefore left a half-printed, entirely normal-LOOKING report on
    the Job Summary page — "could not measure" wearing the costume of
    "measured", which is the one confusion this tool exists to prevent.
    Measured on the pre-change code: a row missing `load_p50` emitted 23 lines
    before dying, ending on a section heading with nothing under it.
    ⚠️ The job itself was already red (the step runs under `set -o pipefail`);
    what this fixes is the Summary page, read by people who do not open the log.
    Buffering makes the output atomic: the whole report, or one ::error:: line.
    ⛔ Catching Exception (not a narrow list) is deliberate — the point is that
    an UNANTICIPATED defect must not reach the page in half-rendered form. The
    known ones are refused earlier, by reject(), with a message that names them.
    """
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = render()
    except Exception as exc:  # noqa: BLE001 - see docstring
        print(f"::error::report generation failed after the input was accepted:"
              f" {type(exc).__name__}: {exc}")
        return 2
    sys.stdout.write(buf.getvalue())
    return rc


def main(argv=None):
    try_utf8_stdout()
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--from-log", metavar="PATH", type=pathlib.Path,
                   help="summarise ONE probe.out as the per-job CI summary (Markdown)")
    g.add_argument("--archive", metavar="DIR", type=pathlib.Path,
                   help="summarise probe-run{1,2,3}.txt in DIR as the archive"
                        " report (text); all three must be present")
    args = ap.parse_args(argv)

    if args.from_log is not None:
        path = args.from_log
        if not path.exists():
            print(f"::error::missing data file {path}")
            return 2
        session = load(path.read_text(), path.name)
        err = reject(session)
        if err:
            print(err)
            return 2
        return emit(lambda: render_ci(session))

    # ⛔ A FIXED file list, not a glob — globbing is not a harmless
    # generalisation here. Two files present would produce a report where the
    # original refused; a fourth file present silently moves every dispatch's
    # "vs the middle dispatch" percentage, because the median of the medians
    # moves; and the CROSS DISPATCH section says "spread of the THREE medians"
    # in hard-coded prose, so both cases would print a false sentence.
    # Generalising to other archive directories is a separate change that has to
    # update that prose too.
    # ⚠️ Inherited, not chosen here: a `probe-run4.txt` in DIR is IGNORED rather
    # than rejected, because the original ignored it. Pinned by a test.
    sessions = []
    for name in RUNS:
        path = args.archive / name
        if not path.exists():
            print(f"::error::missing data file {path}")
            return 2
        session = load(path.read_text(), path.name)
        err = reject(session, thin_msg=lambda s: (
            f"::error::parsed {len(s.rows)} measurement rows, need >= 2"))
        if err:
            print(err.replace("::error::", f"::error::{path.name}: ", 1))
            return 2
        sessions.append(session)

    # ⛔ reject() runs PER FILE, so its iters check cannot see this: the archive
    # renderer POOLS all three files' rows and divides the per-iteration figures
    # by `allrows[0]["iters"]` — one value from one file, applied to all of them.
    # Measured: with run1's rows at iters=800 and the other two at 400, the
    # report printed "across all 800 iterations" while its own identity section
    # printed `iters=400`, rc=0, no warning — a report contradicting itself.
    # ⚠️ Deliberately harder than the neighbouring shape mismatch, which only
    # warns: that one compares PROBEENV text, which is metadata, while this is
    # the divisor. Found by blind review of the first draft, which only guarded
    # the per-file path.
    spread = sorted({r["iters"] for s in sessions for r in s.rows})
    if len(spread) > 1:
        per_run = ", ".join(
            f"{s.name}={sorted({r['iters'] for r in s.rows})}" for s in sessions)
        print(f"::error::runs report differing iters {spread} ({per_run}); the"
              " per-iteration figures pool every run and divide by a single"
              " value, so these cannot be summarised together")
        return 2
    return emit(lambda: render_archive(sessions))


if __name__ == "__main__":
    sys.exit(main())
