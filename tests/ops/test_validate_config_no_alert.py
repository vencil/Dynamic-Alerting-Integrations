"""validate-config `no_alert`（ADR-038 決定 10、#3591）：送出了、但出貨的告警規則都不比較的閾值鍵。

送出的鍵取自共用判決（exporter 讀、/metrics 有送的鍵，帶值來自哪個檔）；有沒有告警比較它，
問 `metric_observed_map.yaml`（`_observed_map_lib.lookup`，由
`check_threshold_observed_map.py` 守住與規則包一致），不另建一份鍵→告警對照。
永遠只是建議：最多 WARN，不影響 exit code。租戶 id 是 fixture 名（CLAUDE.md #9）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import validate_config as vc
from _registry_lib import DEPRECATED_KEY_ALIASES

pytestmark = pytest.mark.usefixtures("da_guard_env")

_REPO = Path(__file__).resolve().parents[2]
_DEFAULTS = ("defaults:\n  mysql_connections: 80\n  mysql_connectoins: 5\n  redis_memory_used_bytes: 70\n"
             "  container_cpu: 50\ndimensions:\n  container_cpu: [version]\n")


def _tree(root: Path, files: dict[str, str]) -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return str(root)


def test_a_served_key_no_alert_compares_is_one_line_on_its_file(tmp_path):
    """根 `defaults:` 的錯字送給每個租戶：一行、掛在 `_defaults.yaml`、列出租戶。
    沒有 critical 層的規則包上的 `<key>_critical` 一樣列出；有比較的、
    observed-map 延後判的（KNOWN_DEFERRED 的 `container_cpu`，含維度鍵）不列。"""
    r = vc.check_no_alert(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "ta.yaml": "tenants:\n  ta:\n    mysql_connections: 70\n    mysql_connections_critical: 90\n",
        "team/tb.yaml": "tenants:\n  tb:\n    redis_memory_used_bytes_critical: 90\n"
                        "    container_cpu{version=\"v2\"}: \"60\"\n"}))
    assert r["status"] == vc.WARN, r
    assert r["details"] == [
        "_defaults.yaml: no_alert `mysql_connectoins` — served, but no shipped alert compares it "
        "— tenant(s) ta, tb",
        "team/tb.yaml: no_alert `redis_memory_used_bytes_critical` — served, but no shipped alert "
        "compares it — tenant(s) tb",
    ]


def test_every_key_compared_passes(tmp_path):
    r = vc.check_no_alert(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "ta.yaml": "tenants:\n  ta:\n    mysql_connections: 70\n"}))
    assert r["status"] == vc.PASS, r


def test_a_retired_spelling_is_looked_up_in_the_canonical_one(tmp_path):
    """舊拼法的鍵照 exporter 的正規拼法查（問 da-guard key-spellings，Python 不留換名規則）：
    正規拼法有告警比較，就不列。"""
    old, new = next(iter(DEPRECATED_KEY_ALIASES.items()))
    r = vc.check_no_alert(_tree(tmp_path, {
        "_defaults.yaml": f"defaults:\n  {new}: 80\n",
        "ta.yaml": f"tenants:\n  ta:\n    {old}: 70\n"}))
    assert r["status"] == vc.PASS, r


def test_a_map_that_cannot_be_read_is_not_checked_never_a_fail(tmp_path, monkeypatch):
    import _observed_map_lib

    def broken(*_a, **_k):
        raise OSError("no map")
    monkeypatch.setattr(_observed_map_lib, "load_observed_map", broken)
    r = vc.check_no_alert(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "ta.yaml": "tenants:\n  ta:\n    mysql_connections: 70\n"}))
    assert r["status"] == vc.WARN and r["details"][0].startswith("not checked:"), r


def test_it_never_changes_the_exit_code(tmp_path):
    """只有 no_alert 有發現的樹：exit 0（WARN），不是 1。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n  mysql_connectoins: 5\n",
        "ta.yaml": "tenants:\n  ta:\n    mysql_connections: \"70\"\n"})
    p = subprocess.run([sys.executable, str(_REPO / "scripts/tools/ops/validate_config.py"),
                        "--config-dir", conf_d, "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    rows = {r["check"]: r for r in json.loads(p.stdout)}
    assert rows["no_alert"]["status"] == vc.WARN, rows["no_alert"]
    assert p.returncode == 0, (p.returncode, [r for r in rows.values() if r["status"] == vc.FAIL])


# The shipped example tree's no_alert keys: rule packs with no critical tier
# (#3658). Shrink-only — a key that appears here is new and fails; a key #3658
# fixes fails too until it is removed from this list.
_SHIPPED_KNOWN = {
    ("examples/mongo-prod.yaml", "mongodb_connections_current_critical"),  # #3658
    ("examples/mongo-prod.yaml", "mongodb_replication_lag_critical"),      # #3658
    ("examples/redis-prod.yaml", "redis_memory_used_bytes_critical"),      # #3658
}


def test_the_shipped_example_tree_has_only_the_known_gaps():
    r = vc.check_no_alert(str(_REPO / "components/threshold-exporter/config/conf.d"))
    got = set()
    for line in r["details"]:
        file, _, rest = line.partition(": no_alert `")
        got.add((file, rest.partition("`")[0]))
    if r["status"] == vc.PASS:
        got = set()
    assert not any(d.startswith("not checked:") for d in r["details"]), r
    assert got == _SHIPPED_KNOWN, (
        f"new: {sorted(got - _SHIPPED_KNOWN)}; fixed, remove from _SHIPPED_KNOWN: "
        f"{sorted(_SHIPPED_KNOWN - got)}")
