"""每個預設出貨（`arming: default`）的閾值鍵都綁一支 promtool 測試（D-A A2a，#1200 裁決 B）。

綁定不手寫：鍵 `<key>` 的測試就是 `tests/rulepacks/armed-<key>_test.yaml`，鍵的清單、值、
pack 檔與告警都從 registry／規則包取。這支測試替每個鍵檢查：

1. 檔案存在，`rule_files` 只載入該鍵的真 pack。
2. 只有一個測試群組、一個 tenant；`user_threshold` 只有一條（不論寫成 `name{…}` 或
   `{__name__="name",…}`）、帶 pack 讀這個鍵用的 `component`／`metric`、是 warning、
   整段是常數，而且等於 registry 的值。
3. 對一個讀這個鍵的告警，有四種期望：門檻下方且已過 `for:` 仍不響；越過門檻後先
   pending；pending 的那一刻不 firing；之後 firing。
4. promtool 跑過。
5. 把門檻推到極大與極小兩個極端各重播一次，promtool 都要失敗——
   證明「響」與「不響」兩個期望都是門檻決定的，不是 `for:` 還沒滿或波形湊巧。

租戶 id 是 fixture 名（CLAUDE.md #9）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import _registry_lib as lib
import _threshold_alerts

pytestmark = pytest.mark.usefixtures("da_guard_env")

_REPO = Path(__file__).resolve().parents[2]
_DIR = Path(__file__).resolve().parent
_DOC = lib.committed_registry_doc()
_KEYS = lib.registry_keys(_DOC)
_ARMED = sorted(k for k, e in _KEYS.items() if lib.is_chart_armed(e))

_FIRING = 'count by(alertname, tenant) (ALERTS{{alertstate="firing", alertname="{alert}"}})'
_PENDING = 'count by(alertname, tenant) (ALERTS{{alertstate="pending", alertname="{alert}"}})'
_DURATION = re.compile(r"(\d+)([smh])")
_UNIT = {"s": 1, "m": 60, "h": 3600}
#: The tenant a series belongs to; Prometheus accepts spaces around `=`.
_TENANT_LABEL = re.compile(r'(?:tenant|namespace)\s*=\s*"([^"]*)"')
#: A threshold no observed value crosses, in either direction.
_FAR = 1e18


def _seconds(duration) -> int:
    """A Prometheus duration (`4m`, `5m30s`, `30s`) in seconds; a bare number is seconds."""
    if isinstance(duration, (int, float)):
        return int(duration)
    parts = _DURATION.findall(str(duration))
    assert parts and "".join(n + u for n, u in parts) == str(duration), duration
    return sum(int(n) * _UNIT[u] for n, u in parts)


def _expanded(values: str) -> list[float]:
    """promtool's expanding notation for constant runs (`80x41`, `79x20 81x20`, `80 80`)."""
    out: list[float] = []
    for token in str(values).split():
        m = re.fullmatch(r"(-?[0-9.eE+]+)x(\d+)", token)
        if m:
            out += [float(m.group(1))] * (int(m.group(2)) + 1)
        else:
            out.append(float(token))
    return out


def _path(key: str) -> Path:
    return _DIR / f"armed-{key}_test.yaml"


def _pack_rules(key: str) -> dict[str, dict]:
    pack = _REPO / _DOC["packs"][_KEYS[key]["pack"]]["rule_pack_file"]
    doc = yaml.safe_load(pack.read_text(encoding="utf-8"))
    return {r["alert"]: r for g in doc["groups"] for r in g["rules"] if "alert" in r}


def _metric_name(series: str) -> str:
    """The metric a promtool input series feeds, however it is spelled:
    `name{...}` or `{__name__="name", ...}`."""
    m = re.search(r'__name__\s*=\s*"([^"]*)"', series)
    return m.group(1) if m else series.partition("{")[0].strip()


def _threshold_series(group: dict) -> list[dict]:
    return [s for s in group["input_series"] if _metric_name(s["series"]) == "user_threshold"]


def _threshold_selector(key: str) -> dict[str, str]:
    """The `component` / `metric` labels this key's threshold is read under:
    what the pack's `…:alert_threshold:<key>` recording rule selects."""
    pack = _REPO / _DOC["packs"][_KEYS[key]["pack"]]["rule_pack_file"]
    doc = yaml.safe_load(pack.read_text(encoding="utf-8"))
    exprs = [r["expr"] for g in doc["groups"] for r in g["rules"]
             if str(r.get("record", "")).endswith(f":alert_threshold:{key}")]
    assert len(exprs) == 1, f"{pack.name} has {len(exprs)} `…:alert_threshold:{key}` recording rules"
    m = re.search(r"user_threshold\{([^}]*)\}", exprs[0])
    assert m, exprs[0]
    labels = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', m.group(1)))
    assert {"component", "metric"} <= set(labels), labels
    return {"component": labels["component"], "metric": labels["metric"]}


def _bound_alert(key: str, group: dict) -> tuple[str, int]:
    """The alert this file exercises: one that reads *key*, with its `for:` in seconds."""
    alerts = _threshold_alerts.alerts_for_key(key)
    assert alerts, f"no shipped alert reads {key}"
    rules = _pack_rules(key)
    exprs = [t["expr"] for t in group.get("promql_expr_test", [])]
    bound = [a for a in alerts if _FIRING.format(alert=a) in exprs]
    assert len(bound) == 1, (f"{_path(key).name} must test exactly one of the alerts that read "
                             f"{key} ({', '.join(alerts)}) through the expression "
                             f"{_FIRING.format(alert='<alert>')}; found {bound}")
    return bound[0], _seconds(rules[bound[0]].get("for", 0))


def _promtool(path: Path) -> subprocess.CompletedProcess:
    exe = shutil.which("promtool")
    assert exe, "promtool is not on PATH (session-start.sh / scripts/ops/install-promtool.sh installs it)"
    return subprocess.run([exe, "test", "rules", str(path)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)


def _is_expectation_mismatch(output: str, alert: str) -> bool:
    """promtool's report of an unmet expectation on *alert*'s `ALERTS` query —
    not a file it could not parse, nor a rule file it did not find."""
    if "no file match pattern" in output:
        return False
    return re.search(r'expr: .*ALERTS\{[^\n]*alertname=\\"' + re.escape(alert)
                     + r'\\"[^\n]*\n\s+exp: [^\n]*\n\s+got: ', output) is not None


def _with_threshold(key: str, value: float, tmp_path: Path) -> Path:
    """This key's test with its one threshold series set to *value* throughout."""
    doc = yaml.safe_load(_path(key).read_text(encoding="utf-8"))
    doc["rule_files"] = [str((_DIR / f).resolve()) for f in doc["rule_files"]]
    (series,) = _threshold_series(doc["tests"][0])
    series["values"] = f"{value}x{len(_expanded(series['values'])) - 1}"
    out = tmp_path / f"armed-{key}_test.yaml"
    out.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return out


def test_the_registry_ships_something_armed():
    """下面的參數化都靠這個清單：空了就是什麼都沒測。"""
    assert _ARMED


def test_no_armed_test_file_is_left_for_a_key_that_is_not_armed():
    """反方向：`armed-<key>_test.yaml` 只給預設出貨的鍵。鍵降回 operator 或被移除後，
    留下的檔案會讓人以為那個值還有測試綁著。"""
    stray = sorted(p.name for p in _DIR.glob("armed-*_test.yaml")
                   if p.name[len("armed-"):-len("_test.yaml")] not in _ARMED)
    assert not stray, stray


@pytest.mark.parametrize("key", _ARMED)
def test_the_file_loads_the_real_pack_for_one_tenant(key):
    path = _path(key)
    assert path.is_file(), (f"{key} ships armed (arming: default) but has no "
                            f"tests/rulepacks/{path.name}: write one (see a sibling armed-*_test.yaml)")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    pack = _DOC["packs"][_KEYS[key]["pack"]]["rule_pack_file"]
    assert doc["rule_files"] == [f"../../{pack}"], doc["rule_files"]
    assert len(doc["tests"]) == 1, "one test group: the same tenant, below then above"
    tenants = {t for s in doc["tests"][0]["input_series"]
               for t in _TENANT_LABEL.findall(s["series"])}
    assert len(tenants) == 1, f"every input series belongs to one tenant, found {sorted(tenants)}"


@pytest.mark.parametrize("key", _ARMED)
def test_the_fixture_threshold_is_the_registry_value(key):
    """fixture 的門檻等於 registry 的值：只有一條 `user_threshold`、warning、整段常數。"""
    group = yaml.safe_load(_path(key).read_text(encoding="utf-8"))["tests"][0]
    series = _threshold_series(group)
    assert len(series) == 1, [s["series"] for s in series]
    # …and it is THIS key's threshold: the labels the pack reads it under.
    labels = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', series[0]["series"]))
    want = {**_threshold_selector(key), "severity": "warning"}
    assert {k: labels.get(k) for k in want} == want, (series[0]["series"], want)
    values = set(_expanded(series[0]["values"]))
    assert values == {float(_KEYS[key]["value"])}, (
        f"{_path(key).name} tests threshold {sorted(values)}, the registry ships "
        f"{_KEYS[key]['value']} for {key}")


@pytest.mark.parametrize("key", _ARMED)
def test_it_expects_silence_below_pending_then_firing_above(key):
    group = yaml.safe_load(_path(key).read_text(encoding="utf-8"))["tests"][0]
    alert, for_s = _bound_alert(key, group)
    tests = group["promql_expr_test"]

    def times(expr: str, *, empty: bool) -> list[int]:
        return sorted(_seconds(t["eval_time"]) for t in tests
                      if t["expr"] == expr and (not t.get("exp_samples")) == empty)
    firing, pending = _FIRING.format(alert=alert), _PENDING.format(alert=alert)
    silent, fired, waiting = times(firing, empty=True), times(firing, empty=False), times(pending, empty=False)
    assert silent and fired and waiting, (silent, fired, waiting)
    # Silence that outlasts `for:` — not the wait before any alert can fire.
    assert silent[0] > for_s, f"the first silent check ({silent[0]}s) is not past for: ({for_s}s)"
    # Pending and not firing at the same instant, after the silent phase, before firing.
    assert any(silent[0] < t < fired[0] and t in silent for t in waiting), (silent, waiting, fired)


@pytest.mark.parametrize("key", _ARMED)
def test_promtool_passes(key):
    r = _promtool(_path(key))
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("key", _ARMED)
@pytest.mark.parametrize("far", [_FAR, -_FAR], ids=["far-above", "far-below"])
def test_moving_the_threshold_turns_it_red(key, far, tmp_path):
    """門檻推到兩個極端各重播一次，都要失敗：一個極端讓告警永遠不響（打掉「響」的期望），
    另一個讓它從頭就響（打掉「門檻下方不響」的期望）。哪個極端是哪一邊取決於告警的比較
    方向，這裡不需要知道——兩邊都得紅。"""
    r = _promtool(_with_threshold(key, far, tmp_path))
    # A failed expectation on the bound alert — promtool prints `FAILED:` for
    # a file it cannot parse or a rule file it cannot find as well, so the
    # evidence is the alert's own `expr … exp: … got: …` block.
    out = r.stdout + r.stderr
    group = yaml.safe_load(_path(key).read_text(encoding="utf-8"))["tests"][0]
    alert, _ = _bound_alert(key, group)
    assert r.returncode == 0 or _is_expectation_mismatch(out, alert), out
    assert r.returncode != 0, (
        f"{_path(key).name} still passes with the {key} threshold at {far}: one of its "
        "expectations (firing, or silent below the threshold) does not depend on the threshold")


def test_a_load_error_is_not_an_expectation_mismatch(tmp_path):
    """重播的失敗要是「期望不符」：promtool 對解析不了的檔、找不到的規則檔也印 `FAILED:`。"""
    key = _ARMED[0]
    alert, _ = _bound_alert(key, yaml.safe_load(_path(key).read_text(encoding="utf-8"))["tests"][0])
    replay = _with_threshold(key, _FAR, tmp_path)
    assert _is_expectation_mismatch((lambda r: r.stdout + r.stderr)(_promtool(replay)), alert)
    broken = tmp_path / "broken_test.yaml"
    broken.write_text("rule_files:\n  - x.yaml\ntests: [\n", encoding="utf-8")
    r = _promtool(broken)
    assert r.returncode != 0 and not _is_expectation_mismatch(r.stdout + r.stderr, alert)
    doc = yaml.safe_load(replay.read_text(encoding="utf-8"))
    doc["rule_files"] = [str(tmp_path / "no-such-pack.yaml")]
    norule = tmp_path / "norule_test.yaml"
    norule.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    r = _promtool(norule)
    assert r.returncode != 0 and not _is_expectation_mismatch(r.stdout + r.stderr, alert)


def test_a_tenant_label_written_with_spaces_is_still_a_tenant():
    """`tenant = "other"` 是合法的 selector 寫法：單一 tenant 的檢查要認得。"""
    series = ['m{tenant="t-armed"}', 'm{tenant = "other", instance="db-1"}', 'm{namespace= "db-x"}']
    assert {t for s in series for t in _TENANT_LABEL.findall(s)} == {"t-armed", "other", "db-x"}
