"""test_threshold_alerts.py — threshold key → 告警名，從 rule pack 反查。

config-diff 的 Affected Alerts 欄與 patch-config --diff 過去用 key 的拼法
猜告警名（mysql_connections → *MysqlConnections*），猜出來的名字 rule pack
裡不存在。這裡釘住三件事：
  1. 名字取自 rule pack，含只經 recording rule 間接讀 key 的告警；
  2. 找不到 rule pack 時回「不知道」（None），不回空集合、不猜；
  3. image 出貨的 pack 清單與 repo 裡引用閾值的 pack 一致——少出貨一個，
     該 pack 的 key 會被報成「沒有告警讀它」，那是 fail-OPEN。
"""
import os
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "lint"))

import _threshold_alerts as ta  # noqa: E402

REPO_PACKS = sorted((REPO / "rule-packs").glob("rule-pack-*.yaml"))


@pytest.fixture
def fresh_index(monkeypatch):
    """每個測試重新載入索引（模組層快取會跨測試殘留）。"""
    monkeypatch.setattr(ta, "_INDEX_CACHE", None)
    monkeypatch.setattr(ta, "_INDEX_LOADED", False)
    monkeypatch.setattr(ta, "_WARNED", False)


class TestRealRulePacks:

    def test_a_key_maps_to_the_alerts_the_pack_names(self, fresh_index):
        assert ta.alerts_for_key("mysql_connections") == (
            "MariaDBHighConnections", "MariaDBSystemBottleneck")

    def test_the_critical_sibling_is_a_different_key(self, fresh_index):
        """`mysql_connections` 不可吃到 `mysql_connections_critical` 的告警。"""
        assert ta.alerts_for_key("mysql_connections_critical") == (
            "MariaDBHighConnectionsCritical",)
        assert "MariaDBHighConnectionsCritical" not in ta.alerts_for_key(
            "mysql_connections")

    @pytest.mark.parametrize("key", [
        "container_cpu", "container_cpu_throttle", "container_memory"])
    def test_keys_compared_inside_a_recording_rule_still_reach_alerts(
            self, fresh_index, key):
        """這三個 key 沒有任何告警直接引用，只看告警 expr 會報成沒人讀。"""
        assert ta.alerts_for_key(key)

    def test_a_dimensional_key_reaches_its_base_keys_alerts(self, fresh_index):
        assert ta.alerts_for_key('mysql_connections{instance="a"}') == \
            ta.alerts_for_key("mysql_connections")

    def test_a_key_no_alert_reads_is_empty_not_unknown(self, fresh_index):
        assert ta.alerts_for_key("no_such_threshold_key") == ()


class TestIndexFromSyntheticPacks:

    def _write(self, path, body):
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    def test_an_alert_reading_a_recording_that_compares_the_key(self, tmp_path):
        pack = self._write(tmp_path / "rule-pack-x.yaml", """\
            groups:
              - name: g
                rules:
                  - record: tenant:alert_threshold:x_load
                    expr: max by(tenant) (user_threshold{metric="load"})
                  - record: x:load_high:core
                    expr: tenant:x_load:max > on(tenant) tenant:alert_threshold:x_load
                  - alert: XLoadHigh
                    expr: x:load_high:core
                  - alert: XDirect
                    expr: tenant:x:max > on(tenant) tenant_version:alert_threshold:x_other
            """)
        index = ta.build_index([pack])
        assert index == {"x_load": ("XLoadHigh",), "x_other": ("XDirect",)}

    def test_recording_cycles_terminate(self, tmp_path):
        pack = self._write(tmp_path / "rule-pack-x.yaml", """\
            groups:
              - name: g
                rules:
                  - record: a:b
                    expr: c:d + tenant:alert_threshold:k
                  - record: c:d
                    expr: a:b
                  - alert: A
                    expr: c:d > 1
            """)
        assert ta.build_index([pack]) == {"k": ("A",)}


class TestLocatingThePacks:

    def test_flat_layout_wins(self, tmp_path):
        (tmp_path / "rule-pack-a.yaml").write_text("groups: []\n", encoding="utf-8")
        assert ta.find_rule_pack_paths(tmp_path) == [tmp_path / "rule-pack-a.yaml"]

    def test_repo_layout_is_found_by_marker(self, tmp_path):
        (tmp_path / "Makefile").write_text("", encoding="utf-8")
        (tmp_path / "rule-packs").mkdir()
        (tmp_path / "rule-packs" / "rule-pack-a.yaml").write_text(
            "groups: []\n", encoding="utf-8")
        tools = tmp_path / "scripts" / "tools" / "ops"
        tools.mkdir(parents=True)
        assert ta.find_rule_pack_paths(tools) == [
            tmp_path / "rule-packs" / "rule-pack-a.yaml"]

    def test_this_checkout_finds_the_repo_packs(self):
        assert ta.find_rule_pack_paths() == REPO_PACKS

    def test_no_packs_means_unknown_and_one_warning(
            self, fresh_index, monkeypatch, capsys):
        monkeypatch.setattr(ta, "find_rule_pack_paths", lambda here=None: [])
        assert ta.alerts_for_key("mysql_connections") is None
        assert ta.alerts_for_key("pg_connections") is None
        err = capsys.readouterr().err
        assert err.count("affected alerts unknown") == 1


class TestImageShipsEveryPackThatReadsAThreshold:
    """image 裡 _threshold_alerts 讀的是 build.sh 搬進去的 pack。"""

    def _threshold_packs(self):
        return {p.name for p in REPO_PACKS
                if "alert_threshold:" in p.read_text(encoding="utf-8")}

    def test_build_sh_ships_them(self):
        from _lint_helpers import BUILD_SH_PATH, parse_build_sh_repo_data_files
        shipped = {os.path.basename(p)
                   for p in parse_build_sh_repo_data_files(BUILD_SH_PATH)
                   if os.path.basename(p).startswith("rule-pack-")}
        assert shipped == self._threshold_packs()

    def test_the_completeness_lint_pairs_them(self):
        from check_build_completeness import REQUIRED_DATA_FILES
        assert set(REQUIRED_DATA_FILES["_threshold_alerts.py"]) == \
            self._threshold_packs()
