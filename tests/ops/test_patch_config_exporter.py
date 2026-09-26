#!/usr/bin/env python3
"""patch-config apply 對「真 exporter」的寫後驗收與回滾（#1950 路線 E）。

build `components/threshold-exporter/app`，以 `-config-dir <tmp> -reload-interval 1s`
起在 127.0.0.1；PATH 上放一支 fake `kubectl`：`get configmap` 讀那個目錄、
`patch` 寫進那個目錄（`os.replace`）、`get pods` 回一個 pod、`get --raw
…/pods/<pod>:<port>/proxy/<path>` 轉成對 localhost 的 HTTP GET。patch_config.py
以子行程跑，所以結束碼、stdout／stderr 都是真的。

沒有 `go` 時 skip 並寫明理由；CI（python-tests-run）設 `VIBE_REQUIRE_GO=1`，
那時缺 go 是失敗而不是 skip。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "components" / "threshold-exporter" / "app"
TOOL = REPO / "scripts" / "tools" / "ops" / "patch_config.py"
_HAS_GO = shutil.which("go") is not None
_REQUIRE_GO = os.environ.get("VIBE_REQUIRE_GO") == "1"
needs_go = pytest.mark.skipif(
    not _HAS_GO, reason="go not on PATH: cannot build the threshold-exporter "
    "these tests run against (set up Go, or VIBE_REQUIRE_GO=1 to fail instead)")

FAKE_KUBECTL = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys, urllib.request
    d, port, a = os.environ["FAKE_CM_DIR"], os.environ["FAKE_PORT"], sys.argv[1:]
    rv_file = os.environ["FAKE_RV"]  # resourceVersion, outside the watched dir
    rv = open(rv_file, encoding="utf-8").read() if os.path.exists(rv_file) else "1"
    if a[:2] == ["get", "configmap"]:
        data = {}
        for n in sorted(os.listdir(d)):
            with open(os.path.join(d, n), encoding="utf-8") as fh:
                data[n] = fh.read()
        print(json.dumps({"metadata": {"resourceVersion": rv}, "data": data}))
    elif a[:1] == ["patch"]:
        with open(a[a.index("--patch-file") + 1], encoding="utf-8") as fh:
            body = json.load(fh)
        patch = body["data"]
        if body.get("metadata", {}).get("resourceVersion") not in (None, rv):
            sys.exit("Error from server (Conflict): the object has been modified")
        for k, v in patch.items():
            p = os.path.join(d, k)
            if v is None:
                os.remove(p)
            else:
                with open(p + ".tmp", "w", encoding="utf-8") as fh:
                    fh.write(v)
                os.replace(p + ".tmp", p)
        rv = str(int(rv) + 1)
        with open(rv_file, "w", encoding="utf-8") as fh:
            fh.write(rv)
        print(json.dumps({"metadata": {"resourceVersion": rv}}))
    elif a[:2] == ["get", "pods"]:
        print(json.dumps({"items": [{"metadata": {"name": "exporter-0"},
              "status": {"phase": "Running"},
              "spec": {"containers": [{"ports": [
                  {"name": "http", "containerPort": int(port)}]}]}}]}))
    elif a[:2] == ["get", "--raw"]:
        path = a[2].split("/proxy/", 1)[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/{path}", timeout=5) as r:
            sys.stdout.write(r.read().decode())
    else:
        sys.exit(f"fake kubectl: unexpected {a}")
''')


def test_go_present_when_required():
    """Fail-closed: 在要求 go 的 job 裡缺 go，下面每一格都會靜默 skip。"""
    if _REQUIRE_GO:
        assert _HAS_GO, "VIBE_REQUIRE_GO=1 but `go` is not on PATH"


@pytest.fixture(scope="module")
def exporter_bin(tmp_path_factory):
    out = tmp_path_factory.mktemp("exporter") / "threshold-exporter"
    subprocess.run(["go", "build", "-buildvcs=false", "-o", str(out), "."],
                   cwd=APP, check=True, timeout=600)
    return out


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/{path}", timeout=5) as r:
        return r.read().decode()


def _last_reload(port):
    return [ln for ln in _get(port, "api/v1/config").splitlines()
            if ln.startswith("Last reload:")][0]


class Cluster:
    def __init__(self, bin_path, tmp_path, files):
        self.dir = tmp_path / "conf.d"
        self.dir.mkdir()
        for name, text in files.items():
            (self.dir / name).write_text(text, encoding="utf-8")
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "kubectl").write_text(FAKE_KUBECTL, encoding="utf-8")
        (bindir / "kubectl").chmod(0o755)
        self.port = _free_port()
        env = {k: v for k, v in os.environ.items()
               if k not in ("CONFIG_DIR", "CONFIG_PATH", "LISTEN_ADDR")}
        self.log = open(tmp_path / "exporter.log", "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [str(bin_path), "-config-dir", str(self.dir), "-listen",
             f"127.0.0.1:{self.port}", "-reload-interval", "1s"],
            env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.env = dict(env, FAKE_CM_DIR=str(self.dir), FAKE_PORT=str(self.port),
                        FAKE_RV=str(tmp_path / "rv"),
                        PATH=f"{bindir}{os.pathsep}{env.get('PATH', '')}")
        deadline = time.monotonic() + 20
        while True:
            try:
                _last_reload(self.port)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        # `Last reload` has one-second resolution: let the startup load's
        # second pass so the patch's reload cannot share it.
        time.sleep(1.1)

    def run(self, *argv):
        return subprocess.run(
            [sys.executable, str(TOOL), *argv, "--poll-interval", "0.2",
             "--reload-timeout", "30"],
            env=self.env, capture_output=True, text=True, timeout=120)

    def user_series(self):
        return sorted(ln for ln in _get(self.port, "metrics").splitlines()
                      if ln.startswith("user_threshold"))

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self.log.close()


@pytest.fixture
def cluster(exporter_bin, tmp_path):
    made = []

    def make(files):
        made.append(Cluster(exporter_bin, tmp_path, files))
        return made[-1]
    yield make
    for c in made:
        c.close()


@needs_go
def test_valid_patch_is_verified_and_served(cluster):
    c = cluster({
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "t-a.yaml": 'tenants:\n  t-a:\n    mysql_connections: "70"\n',
        "t-b.yaml": 'tenants:\n  t-b:\n    mysql_connections: "60"\n',
    })
    r = c.run("t-a", "mysql_connections", "65")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Verified on 1 exporter pod" in r.stdout
    assert ('user_threshold{component="mysql",metric="connections",'
            'severity="warning",tenant="t-a"} 65') in c.user_series()


@needs_go
def test_anchor_shared_block_rolls_back_bytes_and_values(cluster):
    # #1950 列 9：legacy config.yaml 的 anchor 共用區塊，改 t-a 會連帶改 t-b。
    # dimensional key：-config-dir 會剝掉 legacy defaults（列 6），base row 不發。
    old = ('tenants:\n  t-a: &shared\n    mysql_connections{db="x"}: "50"\n'
           '  t-b: *shared\n')
    c = cluster({"config.yaml": old})
    before = c.user_series()
    assert any('tenant="t-b"' in s and s.endswith(" 50") for s in before), before

    r = c.run("t-a", 'mysql_connections{db="x"}', "60")
    assert r.returncode == 1, r.stdout + r.stderr
    assert 'tenant="t-b"' in r.stderr and "another tenant" in r.stderr
    assert (c.dir / "config.yaml").read_text(encoding="utf-8") == old
    assert c.user_series() == before


@needs_go
def test_silent_mode_disable_is_applied_and_listed(cluster):
    # 方案 C：`_` key 的目標租戶 series 不判，只列出；user_silent_mode 消失不回滾。
    c = cluster({
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "t-a.yaml": ('tenants:\n  t-a:\n    mysql_connections: "70"\n'
                     "    _silent_mode: warning\n"),
    })
    silent = 'user_silent_mode{target_severity="warning",tenant="t-a"} 1'
    assert silent in _get(c.port, "metrics").splitlines()
    r = c.run("--json", "t-a", "_silent_mode", "disable")
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert (doc["status"], doc["written"]) == ("applied", True)
    assert {"series": silent.rsplit(" ", 1)[0], "before": "1", "after": None} \
        in doc["pods"]["exporter-0"]["target_changes"]
    assert silent not in _get(c.port, "metrics").splitlines()
