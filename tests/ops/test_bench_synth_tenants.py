"""`scripts/ops/bench_synth_tenants.py` — benchmark.sh 的合成租戶注入／清除。

測函式本身，以及 CLI 對一個會記住狀態的假 `kubectl`（PATH shim，套 JSON
merge patch）的行為；另測 benchmark.sh 的 `--tenants` 參數檢查。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "ops" / "bench_synth_tenants.py"

_spec = importlib.util.spec_from_file_location("bench_synth_tenants", SCRIPT)
bst = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bst)

import patch_config as pc  # noqa: E402  (tests/conftest.py puts ops/ on sys.path)


def _cm(**data):
    return {"data": data}


# ── pure functions ─────────────────────────────────────────────────────


def test_one_key_per_tenant_and_the_key_stem_is_the_tenant():
    carriers = bst.build_synth_carriers(3)
    assert sorted(carriers) == ["synth-0000.yaml", "synth-0001.yaml",
                                "synth-0002.yaml"]
    for key, text in carriers.items():
        doc = yaml.safe_load(text)
        assert list(doc) == ["tenants"]
        assert list(doc["tenants"]) == [key[:-len(".yaml")]]


def test_injected_keys_are_located_by_the_patch_config_locator():
    """Stem-based readers and the content-based locator agree on every key."""
    cm = _cm(**{"_defaults.yaml": "defaults: {}"}, **bst.build_synth_carriers(4))
    for i in range(4):
        name = bst.synth_tenant_name(i)
        assert pc.locate_tenant_key(cm, name) == f"{name}.yaml"


def test_clean_configmap_has_no_conflicts():
    cm = _cm(**{"_defaults.yaml": "defaults: {}",
                "tenant-x.yaml": "tenants:\n  tenant-x: {cpu: '1'}\n"})
    assert bst.injection_conflicts(cm, ["synth-0000.yaml"]) == []


def test_existing_same_named_key_is_a_conflict():
    cm = _cm(**{"_defaults.yaml": "defaults: {}", "synth-0000.yaml": ""})
    problems = bst.injection_conflicts(cm, ["synth-0000.yaml", "synth-0001.yaml"])
    assert problems == ["key synth-0000.yaml already exists"]


def test_stale_thresholds_yaml_declaring_synth_tenants_is_a_conflict():
    """名字與新 key 不撞、但內容宣告了 synth-* 的 key。"""
    stale = yaml.safe_dump({"tenants": {"synth-0007": {"cpu": "1"}}})
    cm = _cm(**{"_defaults.yaml": "defaults: {}", "thresholds.yaml": stale})
    problems = bst.injection_conflicts(cm, ["synth-0000.yaml"])
    assert problems == ["tenant synth-0007 is already declared by thresholds.yaml"]


def test_unparseable_carrier_refuses_injection():
    cm = _cm(**{"_defaults.yaml": "defaults: {}", "broken.yaml": "a: [\n"})
    with pytest.raises(pc.ConfigMapShapeError):
        bst.injection_conflicts(cm, ["synth-0000.yaml"])


def test_cleanup_patch_nulls_exactly_the_injected_keys():
    assert bst.cleanup_patch(["synth-0000.yaml", "synth-0001.yaml"]) == {
        "data": {"synth-0000.yaml": None, "synth-0001.yaml": None}}


# ── CLI against a stateful fake kubectl ────────────────────────────────

_FAKE_KUBECTL = r'''
import json, sys
state_path, mode = sys.argv[1], sys.argv[2]
fail_patch, ignore_patch = mode == "1", mode == "2"
argv = sys.argv[3:]
cm = json.load(open(state_path, encoding="utf-8"))
if argv[0] == "get":
    print(json.dumps(cm)); sys.exit(0)
if argv[0] == "patch":
    if fail_patch:
        print("simulated apiserver error", file=sys.stderr); sys.exit(1)
    if ignore_patch:
        sys.exit(0)
    patch = json.load(open(argv[argv.index("--patch-file") + 1], encoding="utf-8"))
    for k, v in patch["data"].items():
        if v is None:
            cm["data"].pop(k, None)
        else:
            cm["data"][k] = v
    json.dump(cm, open(state_path, "w", encoding="utf-8")); sys.exit(0)
sys.exit(2)
'''


@pytest.fixture
def cluster(tmp_path):
    """A fake cluster: `state` is the ConfigMap JSON on disk; `run(...)`."""
    state = tmp_path / "cm.json"
    impl = tmp_path / "fake_kubectl.py"
    impl.write_text(_FAKE_KUBECTL, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    flag = tmp_path / "fail_patch"
    flag.write_text("0", encoding="utf-8")
    shim = bin_dir / "kubectl"
    shim.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "{state}" '
        f'"$(cat "{flag}")" "$@"\n', encoding="utf-8", newline="\n")
    shim.chmod(0o755)

    class Cluster:
        def set(self, **data):
            state.write_text(json.dumps({"data": data}), encoding="utf-8")

        def data(self):
            return json.loads(state.read_text(encoding="utf-8"))["data"]

        def fail_patches(self, mode="1"):
            flag.write_text(mode, encoding="utf-8")

        def run(self, *args):
            env = dict(os.environ,
                       PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
            return subprocess.run([sys.executable, str(SCRIPT), *args],
                                  capture_output=True, text=True, env=env,
                                  timeout=60)
    return Cluster()


_posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX kubectl shim")


@_posix
def test_inject_then_cleanup_round_trip(cluster, tmp_path):
    cluster.set(**{"_defaults.yaml": "defaults: {}",
                   "tenant-x.yaml": "tenants:\n  tenant-x: {cpu: '1'}\n"})
    state = tmp_path / "keys"
    r = cluster.run("inject", "--tenants", "3", "--state-file", str(state))
    assert r.returncode == 0, r.stderr
    assert sorted(cluster.data()) == ["_defaults.yaml", "synth-0000.yaml",
                                      "synth-0001.yaml", "synth-0002.yaml",
                                      "tenant-x.yaml"]
    r = cluster.run("cleanup", "--state-file", str(state))
    assert r.returncode == 0, r.stderr
    assert sorted(cluster.data()) == ["_defaults.yaml", "tenant-x.yaml"]


@_posix
def test_inject_refuses_over_a_stale_thresholds_yaml_and_writes_nothing(cluster, tmp_path):
    stale = yaml.safe_dump({"tenants": {"synth-0000": {"cpu": "1"}}})
    cluster.set(**{"_defaults.yaml": "defaults: {}", "thresholds.yaml": stale})
    state = tmp_path / "keys"
    r = cluster.run("inject", "--tenants", "2", "--state-file", str(state))
    assert r.returncode != 0
    assert "thresholds.yaml" in r.stderr
    assert sorted(cluster.data()) == ["_defaults.yaml", "thresholds.yaml"]
    assert not state.exists()


@_posix
def test_cleanup_failure_is_loud(cluster, tmp_path):
    """清除失敗 ⇒ rc 非零、stderr 具名，合成租戶的 key 仍在叢集裡。"""
    cluster.set(**{"_defaults.yaml": "defaults: {}"})
    state = tmp_path / "keys"
    assert cluster.run("inject", "--tenants", "2",
                       "--state-file", str(state)).returncode == 0
    cluster.fail_patches()
    r = cluster.run("cleanup", "--state-file", str(state))
    assert r.returncode != 0
    assert "simulated apiserver error" in r.stderr
    assert "synth-0000.yaml" in cluster.data()


@_posix
def test_cleanup_that_leaves_keys_behind_is_loud(cluster, tmp_path):
    """patch 回 rc 0 但 key 還在（例如被別的 writer 蓋回來）⇒ 回讀驗證要紅。"""
    cluster.set(**{"_defaults.yaml": "defaults: {}"})
    state = tmp_path / "keys"
    assert cluster.run("inject", "--tenants", "2",
                       "--state-file", str(state)).returncode == 0
    cluster.fail_patches("2")
    r = cluster.run("cleanup", "--state-file", str(state))
    assert r.returncode != 0
    assert "still in" in r.stderr


@_posix
def test_state_file_is_written_before_the_patch_is_sent(cluster, tmp_path):
    """patch 一送出去就可能已生效；state 檔要在那之前寫好，trap 才知道要刪哪些。"""
    cluster.set(**{"_defaults.yaml": "defaults: {}"})
    cluster.fail_patches()
    state = tmp_path / "keys"
    r = cluster.run("inject", "--tenants", "2", "--state-file", str(state))
    assert r.returncode != 0
    assert state.read_text(encoding="utf-8").split() == ["synth-0000.yaml",
                                                         "synth-0001.yaml"]


@_posix
@pytest.mark.parametrize("n", ["0", "-1"])
def test_inject_rejects_fewer_than_one_tenant(cluster, tmp_path, n):
    cluster.set(**{"_defaults.yaml": "defaults: {}"})
    state = tmp_path / "keys"
    r = cluster.run("inject", "--tenants", n, "--state-file", str(state))
    assert r.returncode == 2
    assert "--tenants" in r.stderr
    assert sorted(cluster.data()) == ["_defaults.yaml"]
    assert not state.exists()


@_posix
def test_inject_rejects_more_than_max_tenants(cluster, tmp_path):
    cluster.set(**{"_defaults.yaml": "defaults: {}"})
    state = tmp_path / "keys"
    r = cluster.run("inject", "--tenants", str(bst.MAX_TENANTS + 1),
                    "--state-file", str(state))
    assert r.returncode == 2
    assert sorted(cluster.data()) == ["_defaults.yaml"]


@_posix
def test_inject_over_an_unreadable_carrier_is_rc_2(cluster, tmp_path):
    cluster.set(**{"_defaults.yaml": "defaults: {}", "broken.yaml": "a: [\n"})
    state = tmp_path / "keys"
    r = cluster.run("inject", "--tenants", "1", "--state-file", str(state))
    assert r.returncode == 2
    assert "broken.yaml" in r.stderr
    assert "Traceback" not in r.stderr
    assert not state.exists()


@_posix
@pytest.mark.parametrize("n", ["010", "08", "0", "abc"])
def test_benchmark_sh_rejects_tenants_that_are_not_a_plain_positive_number(
        tmp_path, n):
    """前導 0 會被 bash 算術當成八進位。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "kubectl"
    shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    shim.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    r = subprocess.run(["bash", str(REPO / "scripts" / "benchmark.sh"),
                        "--tenants", n], capture_output=True, text=True,
                       env=env, timeout=60)
    assert r.returncode == 1
    assert "--tenants must be" in r.stderr
