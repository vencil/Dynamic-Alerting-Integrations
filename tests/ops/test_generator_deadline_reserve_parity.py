"""#2903（ADR-036 §3）：tenant-api 對產生器保留時間的假設，與產生器服務的預設值對帳。

產生器服務從 ``deadline_ms`` 裡保留一段時間（``--deadline-reserve-seconds``，
預設 ``DEFAULT_DEADLINE_RESERVE_SECONDS``）給 tenant-api 核對與 commit。
tenant-api 的 ``generator.ServiceReserve`` 是同一個數字的 Go 副本，
``MinJudgeTime``（剩下多少時間以下就不送、直接回 ``no_time``）由它推得。
兩邊各改一邊時，tenant-api 會把還判得完的寫入拒成 ``no_time``，或把判不完的
送出去等 ``deadline``——都不會紅，所以在這裡對帳。

Go 端用 regex 讀；讀不到（句型改了）就 fail，不當成相符。
"""
from __future__ import annotations

import re
from pathlib import Path

import generator_service as gs  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "components" / "tenant-api" / "internal" / "generator" / "gate.go"
CHART = REPO / "helm" / "tenant-api"

_RESERVE = re.compile(r"^const ServiceReserve = (\d+(?:\.\d+)?) \* time\.Second$", re.M)
_MIN_JUDGE = re.compile(r"^const MinJudgeTime = ServiceReserve \+ (\d+(?:\.\d+)?) \* time\.Second$"
                        r"|^const MinJudgeTime = ServiceReserve \+ time\.Second$", re.M)


def _gate_source() -> str:
    return GATE.read_text(encoding="utf-8")


def test_service_reserve_matches_the_generator_default():
    m = _RESERVE.search(_gate_source())
    assert m, f"cannot read `const ServiceReserve = <n> * time.Second` from {GATE}"
    assert float(m.group(1)) == gs.DEFAULT_DEADLINE_RESERVE_SECONDS, (
        f"tenant-api ServiceReserve is {m.group(1)}s, the generator service keeps "
        f"{gs.DEFAULT_DEADLINE_RESERVE_SECONDS}s (DEFAULT_DEADLINE_RESERVE_SECONDS)")


def test_min_judge_time_is_the_reserve_plus_time_to_judge():
    m = _MIN_JUDGE.search(_gate_source())
    assert m, (f"cannot read `const MinJudgeTime = ServiceReserve + …` from {GATE}: "
               "MinJudgeTime must be derived from ServiceReserve")
    extra = float(m.group(1)) if m.group(1) else 1.0
    assert extra > 0, "MinJudgeTime must leave the generator some time to judge in"


def test_the_chart_does_not_override_the_reserve():
    # ServiceReserve mirrors the DEFAULT; a chart that passes the flag would
    # make the two disagree without either file changing.
    hits = [p for p in CHART.rglob("*") if p.is_file()
            and "deadline-reserve-seconds" in p.read_text(encoding="utf-8", errors="replace")]
    assert not hits, f"the tenant-api chart overrides --deadline-reserve-seconds: {hits}"
