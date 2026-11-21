"""租戶元資料語意矩陣：值寫在哪裡，generate_tenant_metadata 的答案都要與 Go 的 served-values 一致（#2115 0-B／B4）。

讀取端：`generate_tenant_metadata.build_tenant_metadata`（platform-data.json 內嵌的
租戶清單與 `_metadata`、db_type）與其 CLI。

同一組值（`_metadata.owner` 與 `redis_connected_clients: 5`）寫在四個位置之一：根
`defaults:`、平台檔 `tenants:`、租戶檔、子目錄（子樹 `_defaults.yaml` 的
`redis_connected_clients`，加上子目錄裡的租戶檔寫的 owner）。tenant-a 是受測租戶；
tenant-b 是對照組，值永遠寫在自己的租戶檔，四個位置下答案都不變。

`_metadata` 不能寫在根 `defaults:` 裡（exporter 整份丟掉 `_defaults.yaml`，那一格見
fail-closed 的測試），所以「defaults」那格只放閾值鍵，owner 依 Go 為空。

根 `defaults:` 宣告的鍵（`mysql_connections`、`redis_connected_clients`）每個租戶都會
收到：列數（`metric_count`）看 Go 實際發出的鍵（含繼承），不看租戶檔自己寫了什麼。db_type 不推斷，只取 Go 的 `_metadata.db_type`：
tenant-a 沒宣告，鍵再像 mysql 也是空字串；tenant-b 宣告 redis，就是 redis。

舊讀取端（ee778a42 以前）只平鋪讀根目錄租戶檔本身：平台 `tenants:` 的 owner 讀成空字串、
子目錄的租戶整個消失、繼承的鍵不進推斷、exporter 丟掉的檔照讀（fail-open）。

另有幾格：exporter 丟掉的檔（fail-closed rc 2：共用的 conf.d 拒絕點名該檔，#3206）、平面格式檔
（沒有 `tenants:`，Go 列進 `skipped`，WARN 點名）、數字型租戶 id（`010` 是 "010"，
不是 8）、過期的維護時間盒（Go 判定已不在維護中）。

da-guard 由 conftest 的 session fixture 以 `go build` 建出；建不起來就 fail、不 skip。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _lib_tenant_values as tv
import generate_tenant_metadata as gtm

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _builtin_layer import BUILTIN_DEFAULTS  # noqa: E402  (#3832)
import _lib_parse_cache
from _go_refusal import assert_refused, go_refused, refuses  # noqa: E402  (#3206)

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "scripts" / "tools" / "dx" / "generate_tenant_metadata.py"

pytestmark = pytest.mark.usefixtures("da_guard_env")

_BASE = "defaults:\n  mysql_connections: 80\n  redis_connected_clients: 100\n"
_A_PLAIN = "tenants:\n  tenant-a:\n    _silent_mode: disable\n"
_B = ("tenants:\n  tenant-b:\n    redis_connected_clients: 7\n"
      "    _metadata:\n      owner: team-b\n      db_type: redis\n")
#: `redis_connected_clients` 關到 {expires} 為止，之後回落成根的 100（送出）。
_EXPIRING_DISABLE = ("tenants:\n  tenant-a:\n    redis_connected_clients:\n      default: disable\n"
                     "      expires: \"{expires}\"\n")

POSITIONS = {
    "defaults": {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n  redis_connected_clients: 5\n",
        "tenant-a.yaml": _A_PLAIN,
        "tenant-b.yaml": _B,
    },
    "platform-tenants": {
        "_defaults.yaml": _BASE + ("tenants:\n  tenant-a:\n    redis_connected_clients: 5\n"
                                   "    _metadata:\n      owner: team-p\n"),
        "tenant-a.yaml": _A_PLAIN,
        "tenant-b.yaml": _B,
    },
    "tenant-file": {
        "_defaults.yaml": _BASE,
        "tenant-a.yaml": ("tenants:\n  tenant-a:\n    redis_connected_clients: 5\n"
                          "    _metadata:\n      owner: team-t\n"),
        "tenant-b.yaml": _B,
    },
    "subtree": {
        "_defaults.yaml": _BASE,
        "team/_defaults.yaml": "defaults:\n  redis_connected_clients: 5\n",
        "team/tenant-a.yaml": ("tenants:\n  tenant-a:\n    _silent_mode: disable\n"
                               "    _metadata:\n      owner: team-s\n"),
        "tenant-b.yaml": _B,
    },
}

# tenant-a 的 owner：Go 在各位置的答案（defaults 那格寫不了 _metadata）。
_OWNER_A = {"defaults": "", "platform-tenants": "team-p", "tenant-file": "team-t",
            "subtree": "team-s"}


def _tree(root: Path, files: dict[str, str]) -> Path:
    conf_d = root / "conf.d"
    for rel, body in files.items():
        p = conf_d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return conf_d


def _cli(conf_d: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "--config-dir", str(conf_d), *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)


# ── 前提：Go 在四個位置都把值發給 tenant-a ───────────────────────────────

@pytest.mark.parametrize("where", sorted(POSITIONS))
def test_the_oracle_serves_the_values_in_every_position(where, tmp_path):
    served = tv.load_served_values(_tree(tmp_path, POSITIONS[where]))
    assert sorted(served) == ["tenant-a", "tenant-b"], where
    assert served["tenant-a"].values["redis_connected_clients"] == 5.0, where
    assert served["tenant-a"].values["mysql_connections"] == 80.0, where
    assert served["tenant-a"].values["_metadata"]["owner"] == _OWNER_A[where], where
    assert served["tenant-b"].values["redis_connected_clients"] == 7.0, where
    assert served["tenant-b"].values["_metadata"]["owner"] == "team-b", where


# ── 四位置：租戶清單、_metadata、推斷都與 served-values 一致 ───────────────

@pytest.mark.parametrize("where", sorted(POSITIONS))
def test_matches_served_values(where, tmp_path):
    conf_d = _tree(tmp_path, POSITIONS[where])
    served = tv.load_served_values(conf_d)
    meta = gtm.build_tenant_metadata(conf_d)["tenant_metadata"]

    assert sorted(meta) == sorted(served) == ["tenant-a", "tenant-b"], (where, sorted(meta))
    for tenant, t in served.items():
        got, md = meta[tenant], t.values["_metadata"]
        for field in ("owner", "tier", "region", "domain", "db_type"):
            assert got[field] == md[field], (where, tenant, field)
        assert got["metric_count"] == len(t.severities), (where, tenant)

    assert meta["tenant-a"]["owner"] == _OWNER_A[where], where
    assert meta["tenant-b"]["owner"] == "team-b", where              # 對照組不變
    # db_type 只認宣告值：tenant-a 繼承了 mysql_connections 也不推成 mariadb。
    # #3771：db_type 是列表（字串宣告＝一元素），沒宣告是 []。
    assert meta["tenant-a"]["db_type"] == [], where
    assert meta["tenant-b"]["db_type"] == ["redis"], where
    # #3832：內建層的鍵每個租戶都有列，列數一併算進來。
    for tenant in ("tenant-a", "tenant-b"):
        # 繼承的 mysql_connections 與 redis_connected_clients 都算數。
        assert meta[tenant]["metric_count"] == 2 + len(BUILTIN_DEFAULTS), (where, tenant)
        # #4141：不輸出 rule_packs——由發出的鍵推得的包每個租戶都一樣（內建層），
        # 租戶宣告的是 db_type。
        assert "rule_packs" not in meta[tenant], (where, tenant, meta[tenant])


@pytest.mark.parametrize("where", sorted(POSITIONS))
def test_cli_output_is_the_built_metadata(where, tmp_path):
    """CLI 走同一條路：rc 0、stdout 的 tenant_metadata 與 served-values 一致。"""
    conf_d = _tree(tmp_path, POSITIONS[where])
    proc = _cli(conf_d)
    assert proc.returncode == 0, proc.stderr
    meta = json.loads(proc.stdout)["tenant_metadata"]
    assert sorted(meta) == ["tenant-a", "tenant-b"], (where, proc.stdout)
    assert meta["tenant-a"]["owner"] == _OWNER_A[where], where


# ── fail-closed：exporter 丟掉的檔 → rc 2，不產出 ─────────────────────────

_DROPPED = {
    "duplicate key": {
        "_defaults.yaml": _BASE,
        "tenant-a.yaml": ("tenants:\n  tenant-a:\n    redis_connected_clients: 5\n"
                          "    redis_connected_clients: 6\n"),
        "tenant-b.yaml": _B,
    },
    "_metadata under root defaults": {
        "_defaults.yaml": _BASE + "  _metadata:\n    owner: team-d\n",
        "tenant-a.yaml": _A_PLAIN,
        "tenant-b.yaml": _B,
    },
}


@pytest.mark.parametrize("case", sorted(_DROPPED))
def test_a_file_the_exporter_drops_fails_closed(case, tmp_path):
    conf_d = _tree(tmp_path, _DROPPED[case])
    dropped = "tenant-a.yaml" if case == "duplicate key" else "_defaults.yaml"
    assert go_refused(conf_d) == [dropped], case      # 前提：Go 點名這個檔
    with _lib_parse_cache.judging():                  # 前提：Go 丟掉這個檔
        with pytest.raises(tv.ParseFailedError):
            tv.load_served_values(conf_d)
    with refuses(dropped):                            # #3206：共用拒絕
        gtm.build_tenant_metadata(conf_d)

    out = tmp_path / "out.json"
    proc = _cli(conf_d, "--output", str(out))
    assert_refused(proc.returncode, proc.stderr, [dropped])
    assert proc.stdout == "", proc.stdout
    assert not out.exists(), "a run that failed must not write --output"


def test_an_entry_the_exporter_cannot_read_fails_closed(tmp_path):
    """A dangling symlink named like a carrier: the exporter's load cannot read
    it (`unreadable`), so the run fails (rc 2, the shared refusal since
    #3206) and names it. Before #2115 0-B/B4 this tool warned about it from
    its own scan and carried on."""
    from _platform_fs import symlink_or_skip  # noqa: PLC0415

    conf_d = _tree(tmp_path, POSITIONS["tenant-file"])
    symlink_or_skip(conf_d / "no-such-target.yaml", conf_d / "broken.yaml")
    with _lib_parse_cache.judging():
        with pytest.raises(tv.ParseFailedError) as excinfo:           # 前提
            tv.load_served_values(conf_d)
    assert [u.file for u in excinfo.value.unreadable] == ["broken.yaml"]
    proc = _cli(conf_d)
    assert_refused(proc.returncode, proc.stderr, ["broken.yaml"])
    assert proc.stdout == ""


def test_missing_da_guard_fails_closed(tmp_path, monkeypatch):
    conf_d = _tree(tmp_path, POSITIONS["tenant-file"])
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    proc = _cli(conf_d)
    assert proc.returncode == 2, proc.stderr
    assert proc.stdout == ""
    assert proc.stderr.startswith("ERROR: da-guard binary not found"), proc.stderr


# ── 平面格式檔：沒有 tenants: 的檔不是租戶，WARN 點名 ─────────────────────

def test_a_flat_file_is_not_a_tenant_and_is_named(tmp_path):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _BASE,
        "tenant-a.yaml": "redis_connected_clients: 5\n",
        "tenant-b.yaml": _B,
    })
    tree = tv.load_served_tree(conf_d)
    assert sorted(tree.tenants) == ["tenant-b"]                       # 前提
    assert [s.file for s in tree.skipped] == ["tenant-a.yaml"]       # 前提

    proc = _cli(conf_d)
    assert proc.returncode == 0, proc.stderr
    assert sorted(json.loads(proc.stdout)["tenant_metadata"]) == ["tenant-b"]
    assert any(ln.startswith("WARN: tenant-a.yaml: ") for ln in proc.stderr.splitlines()), proc.stderr


# ── 數字型租戶 id：`010:` 是租戶 "010" ──────────────────────────────────

def test_a_numeric_tenant_id_is_its_source_text(tmp_path):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _BASE,
        "010.yaml": "tenants:\n  010:\n    _metadata:\n      owner: team-n\n",
        "tenant-b.yaml": _B,
    })
    served = tv.load_served_values(conf_d)
    assert sorted(served) == ["010", "tenant-b"]                     # 前提
    meta = gtm.build_tenant_metadata(conf_d)["tenant_metadata"]
    assert sorted(meta) == ["010", "tenant-b"], sorted(meta)
    assert meta["010"]["owner"] == served["010"].values["_metadata"]["owner"] == "team-n"


# ── 整天：沒有任何欄位是某一刻的狀態（#4109） ─────────────────────────────

_STATE_FILTERS = ("state_filters:\n  maintenance:\n    reasons: []\n    severity: info\n"
                  "    default_state: disable\n")

#: 01:00-02:00 送出、其餘時段關掉的鍵；以及每週固定時窗的維護。
_WINDOWED = ("tenants:\n  tenant-a:\n    pg_connections:\n      default: disable\n"
             "      overrides:\n        - window: \"01:00-02:00\"\n          value: \"60\"\n"
             "    _state_maintenance:\n      recurring:\n        - weekdays: [fri]\n"
             "          window: \"01:00-02:00\"\n")


def test_the_metadata_is_the_same_at_every_time_of_day(tmp_path):
    """#4109：同一棵樹在時窗內、時窗外、另一個星期幾產生的元資料逐欄相同（`generated`
    除外）。只在某時窗送出的鍵整天都算送出；維護、靜音是某一刻的狀態，不在元資料裡
    （tenant-api 的 config_derived 在被問的那一刻推算）。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _BASE + "  pg_connections: 80\n" + _STATE_FILTERS + (
            "tenants:\n  tenant-b:\n    _silent_mode: warning\n"),
        "tenant-a.yaml": _WINDOWED,
        "tenant-b.yaml": _B,
    })
    # 前提：一般模式在 01:30（週五）與 12:00、週六 01:30 讀到的確實不同。
    inside = tv.load_served_values(conf_d, at="2026-11-20T01:30:00Z")["tenant-a"]
    outside = tv.load_served_values(conf_d, at="2026-11-21T01:30:00Z")["tenant-a"]
    assert inside.values["_state_maintenance"] is True and outside.values["_state_maintenance"] is False
    assert "pg_connections" in inside.values
    assert "pg_connections" not in tv.load_served_values(conf_d, at="2026-11-20T12:00:00Z")["tenant-a"].values
    runs = [gtm.build_tenant_metadata(conf_d, at=at)["tenant_metadata"]
            for at in ("2026-11-20T01:30:00Z", "2026-11-20T12:00:00Z", "2026-11-21T01:30:00Z")]
    assert runs[0] == runs[1] == runs[2], runs
    for tenant, entry in runs[0].items():
        assert "operational_mode" not in entry, (tenant, entry)
    # 對照：同一個鍵整天都關掉時少算一個——只在時窗內送出的鍵確實算進去了。
    off = _tree(tmp_path / "off", {
        "_defaults.yaml": _BASE + "  pg_connections: 80\n" + _STATE_FILTERS,
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    pg_connections: disable\n",
    })
    assert gtm.build_tenant_metadata(off)["tenant_metadata"]["tenant-a"]["metric_count"] == \
        runs[0]["tenant-a"]["metric_count"] - 1, runs[0]["tenant-a"]


def test_routing_channel_is_the_exporters_routing_read_at_no_instant(tmp_path):
    """#4109：`_routing` 取自 `--config-facts`（exporter 模組的 routing resolver，不吃
    時刻），與一般模式的 `routing` 同一份讀法。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _BASE,
        "tenant-a.yaml": ("tenants:\n  tenant-a:\n    _routing:\n      receiver:\n        type: webhook\n"
                          "        url: \"https://hooks.example.com/a\"\n"),
        "tenant-b.yaml": _B,
    })
    served = tv.load_served_values(conf_d)
    assert served["tenant-a"].routing["receiver"]["url"] == "https://hooks.example.com/a"   # 前提
    meta = gtm.build_tenant_metadata(conf_d)["tenant_metadata"]
    assert meta["tenant-a"]["routing_channel"] == "webhook:https://hooks.example.com/a", meta["tenant-a"]
    assert meta["tenant-b"]["routing_channel"] == "", meta["tenant-b"]


def test_check_evaluates_at_the_recorded_instant(tmp_path):
    """`--check` 以檔案記下的 `generated` 時刻重讀，不以現在：限時的 disable 在兩者
    之間到期時，檔案沒變就不能轉紅（#2115 B4）。對照組：同一棵樹在兩個時刻答案不同
    （#4109 起元資料只有 `expires:` 依時刻）。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _BASE,
        "tenant-a.yaml": _EXPIRING_DISABLE.format(expires="2020-01-01T00:00:00Z"),
    })
    before = "2019-12-31T00:00:00Z"
    data = gtm.build_tenant_metadata(conf_d, at=before)
    assert data["generated"] == before
    then = data["tenant_metadata"]["tenant-a"]["metric_count"]
    now = gtm.build_tenant_metadata(conf_d)["tenant_metadata"]["tenant-a"]
    assert now["metric_count"] == then + 1                                           # 前提

    out = tmp_path / "meta.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    proc = _cli(conf_d, "--output", str(out), "--check")
    assert proc.returncode == 0, proc.stderr

    # HEAD commit 不是輸入：換了 commit、檔案沒變，--check 不轉紅。
    data["tenant_metadata"]["tenant-a"]["last_config_commit"] = "0000000"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    proc = _cli(conf_d, "--output", str(out), "--check")
    assert proc.returncode == 0, proc.stderr

    # 必響對照：記下的時刻之後改了值，--check 仍要轉紅。
    data["tenant_metadata"]["tenant-a"]["owner"] = "someone-else"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert _cli(conf_d, "--output", str(out), "--check").returncode == 1

    # tenant_metadata 不是物件：照舊判為 outdated（rc 1、具名），不丟 traceback。
    out.write_text('{"tenant_metadata": [1]}\n', encoding="utf-8")
    proc = _cli(conf_d, "--output", str(out), "--check")
    assert proc.returncode == 1 and "is outdated" in proc.stderr, proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr


def test_check_ignores_a_generated_in_the_future(tmp_path):
    """檔案是在限時 disable 到期後的時刻產的（鍵已送出），`generated` 再挪到更遠的
    未來：若 --check 照收那個時刻，答案不變、過關；應退回現在（還關著）→ 紅。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _BASE,
        "tenant-a.yaml": _EXPIRING_DISABLE.format(expires="2099-01-01T00:00:00Z"),
    })
    data = gtm.build_tenant_metadata(conf_d, at="2099-06-01T00:00:00Z")
    after = data["tenant_metadata"]["tenant-a"]["metric_count"]
    now = gtm.build_tenant_metadata(conf_d)["tenant_metadata"]["tenant-a"]
    assert now["metric_count"] == after - 1                                          # 前提
    data["generated"] = "2100-01-01T00:00:00Z"
    out = tmp_path / "meta.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    proc = _cli(conf_d, "--output", str(out), "--check")
    assert proc.returncode == 1 and "is outdated" in proc.stderr, proc.stderr


@pytest.mark.parametrize("generated, want", [
    ("2019-12-31T00:00:00Z", "2019-12-31T00:00:00Z"),
    ("2100-01-01T00:00:00Z", None),          # 未來
    ("2019-1-1T0:0:0Z", None),               # Go 的 RFC3339 不收
    ("2019-12-31T00:00:00z", None),
    ("2019-12-31 00:00:00Z", None),
    ("2019-12-31T00:00:00+00:00", None),     # 不是本工具寫的格式
    (20191231, None),
])
def test_recorded_at_takes_only_a_past_instant_in_the_written_format(generated, want):
    assert tv.recorded_at({"generated": generated}) == want
    assert tv.recorded_at({}) is None
    assert tv.recorded_at(None) is None


def test_no_value_is_read_from_the_yaml_itself():
    """結構釘：本工具不再自己讀租戶 YAML（`_groups.yaml` 除外，那不是租戶值），也不讀
    served-values 某一刻的值（#4109：整天讀，`load_served_tree` 是某一刻的讀者）。"""
    src = TOOL.read_text(encoding="utf-8")
    assert "load_whole_day(" in src
    for gone in ("strict_load_exporter_keys", "yaml.safe_load", "iterdir(", "load_served_tree("):
        assert gone not in src, gone
    assert os.environ.get("DA_GUARD_BINARY"), "fixture did not set DA_GUARD_BINARY"
