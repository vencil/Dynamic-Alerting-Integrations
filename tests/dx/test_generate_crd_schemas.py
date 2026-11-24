#!/usr/bin/env python3
"""generate_crd_schemas.py 的來源 URL 政策守衛。

這支工具產生的是**閘門的判準**（`check_md_yaml_drift.py --check crd` 拿它來判
文件對錯），所以「schema 從哪來」本身是安全性質，不是風格。兩條規則各自守一個
**會靜默發生**的失效：

  1. **必須 https**。`file://` 會讓判準變成一份沒人 review 的本機檔案；`http://`
     會讓路徑上任何人改寫那份判準。bandit 的 B310 問的就是這件事——本檔是那條
     `# nosec B310` 宣稱的憑據，拿掉驗證而留著 nosec 會讓宣稱變成空話。
  2. **必須釘版本**。`main` / `latest` 今天與明天不是同一份 schema，而閘門不會
     因此出聲：它只會安靜地換一個判準。

⚠️ 兩格都不連網——驗的是判定函式，不是抓取行為。

檔尾另守 #2335：讀 CRD 需要的 `=`（YAML 1.1 `!!value`）constructor 只能掛在
本模組私有的 loader 上，import 本模組不得改變行程裡其他 SafeLoader 的行為。
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = (Path(__file__).resolve().parents[2]
        / "scripts" / "tools" / "dx" / "generate_crd_schemas.py")


def _load():
    spec = importlib.util.spec_from_file_location("generate_crd_schemas", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("generate_crd_schemas", mod)
    spec.loader.exec_module(mod)
    return mod


MODULE = _load()


@pytest.mark.parametrize("url", [
    "https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.0/manifests/crds/application-crd.yaml",
    "https://github.com/cert-manager/cert-manager/releases/download/v1.19.1/cert-manager.crds.yaml",
])
def test_pinned_https_urls_are_accepted(url: str) -> None:
    assert MODULE._validate_source_url(url) is None


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "http://example.invalid/v1.0.0/crd.yaml",
    "/tmp/crd.yaml",
])
def test_non_https_is_rejected(url: str) -> None:
    """The evidence behind this file's `# nosec B310`."""
    err = MODULE._validate_source_url(url)
    assert err is not None, url
    assert "https" in err


@pytest.mark.parametrize("url", [
    "https://raw.githubusercontent.com/o/r/main/crd.yaml",
    "https://raw.githubusercontent.com/o/r/master/crd.yaml",
    "https://raw.githubusercontent.com/o/r/HEAD/crd.yaml",
    "https://github.com/o/r/releases/latest/download/crd.yaml",
])
def test_unpinned_refs_are_rejected(url: str) -> None:
    err = MODULE._validate_source_url(url)
    assert err is not None, url
    assert "pinned" in err


def test_shipped_sources_all_satisfy_the_policy() -> None:
    """Anti-vacuity: the rules above must hold for what the repo actually ships,
    otherwise they are a policy nothing is measured against."""
    yaml = pytest.importorskip("yaml")
    decl = yaml.safe_load(
        (MODULE.CRD_DIR / "SOURCES.yaml").read_text(encoding="utf-8"))
    sources = decl["sources"]
    assert sources, "SOURCES.yaml declares no sources — the check above proves nothing"
    for src in sources:
        assert MODULE._validate_source_url(src["url"]) is None, src["id"]


# ── #2335：`=` 的 constructor 只屬於讀 CRD 的那一處 ──────────────────────
#
# 上游 AlertmanagerConfig CRD 有一行字面 `- =`，YAML 1.1 把它解成 `!!value`
# tag，SafeLoader 預設沒有對應 constructor。這支工具需要讀它，但**不能**為此改
# 全域的 `yaml.SafeLoader`：同一行程裡其他 SafeLoader 系 loader（exporter-key
# loader、strict loader）會跟著開始接受 `=`，而且只在「先 import 過本模組」時
# 才會——測試結果因此依賴 worker 的執行順序（#2334 實際碰到）。

_CRD_WITH_VALUE_TAG = """\
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
spec:
  group: example.com
  names: {kind: Widget}
  versions:
  - name: v1
    schema:
      openAPIV3Schema:
        type: object
        properties:
          op:
            type: string
            enum:
            - =
            - "!="
"""


def test_the_crd_reader_still_accepts_the_value_tag() -> None:
    """The reason the constructor exists must survive moving it off the global."""
    wanted = [{"group": "example.com", "kind": "Widget", "version": "v1"}]
    found, missing = MODULE._schemas_from(_CRD_WITH_VALUE_TAG.encode("utf-8"), wanted)
    assert missing == []
    schema = found[("example.com", "Widget", "v1")]
    assert schema["properties"]["op"]["enum"] == ["=", "!="]


def test_the_crd_reader_cannot_construct_python_objects() -> None:
    """#3206: the CRD is read the way kubectl reads it (da-crdecode, JSON
    out), so a Python tag constructs nothing — the reading is plain data. The
    control beside it is `test_the_crd_reader_still_accepts_the_value_tag`.
    The tag sits in an object: a CRD file is a manifest, whose documents are
    Kubernetes objects (#3426) — a bare top-level list is refused."""
    payload = "kind: CustomResourceDefinition\nx: !!python/object/apply:os.system ['true']\n"
    docs = MODULE._load_crd_docs(payload)
    assert docs == [{"kind": "CustomResourceDefinition", "x": ["true"]}]


def test_importing_the_tool_leaves_the_global_safe_loader_alone() -> None:
    """Run in a FRESH interpreter: this pytest worker has already imported the
    module (MODULE above), so an in-process check would pass or fail depending
    on what else ran first. The `before` row is the anti-vacuity half — if `=`
    were already accepted before the import, the `after` row proves nothing."""
    repo = TOOL.parents[3]
    script = (
        "import json, sys, yaml\n"
        "sys.path[:0] = [sys.argv[1], sys.argv[2]]\n"
        "import _lib_io as IO\n"
        "loaders = {\n"
        "    'safe_load': lambda s: yaml.safe_load(s),\n"
        "    'strict_safe_load': lambda s: IO.strict_safe_load(s),\n"
        "}\n"
        "def probe():\n"
        "    out = {}\n"
        "    for name, fn in loaders.items():\n"
        "        try:\n"
        "            out[name] = repr(fn('a: =\\n'))\n"
        "        except yaml.constructor.ConstructorError:\n"
        "            out[name] = 'ConstructorError'\n"
        "    return out\n"
        "before = probe()\n"
        "import generate_crd_schemas\n"
        "print(json.dumps({'before': before, 'after': probe()}))\n"
    )
    p = subprocess.run(
        [sys.executable, "-c", script,
         str(repo / "scripts" / "tools"), str(repo / "scripts" / "tools" / "dx")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, cwd=str(repo))
    assert p.returncode == 0, p.stderr
    got = json.loads(p.stdout.strip().splitlines()[-1])
    rejected = {name: "ConstructorError" for name in got["before"]}
    assert got["before"] == rejected, got
    assert got["after"] == rejected, got


# ── #3981: main end to end, offline — the write through the artifact entry ──
# `_fetch` (the network) and `_load_crd_docs` (da-crdecode) are replaced; the
# rest — the lock, the per-source loop, the writes — runs as shipped.

def _crd_doc(group: str, kind: str, version: str = "v1") -> dict:
    return {"kind": "CustomResourceDefinition",
            "spec": {"group": group, "names": {"kind": kind},
                     "versions": [{"name": version, "schema": {"openAPIV3Schema": {
                         "type": "object", "description": f"{kind} schema"}}}]}}


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """Two pinned sources under a temp CRD_DIR; ``offline.fail`` names the
    source ids whose fetch fails."""
    crd = tmp_path / "crd"
    crd.mkdir()
    (crd / "SOURCES.yaml").write_text(
        "sources:\n"
        "  - id: one\n    url: https://example.invalid/v1.0.0/one.yaml\n    ref: v1.0.0\n"
        "    kinds: [{group: a.example, kind: Alpha, version: v1}]\n"
        "  - id: two\n    url: https://example.invalid/v1.0.0/two.yaml\n    ref: v1.0.0\n"
        "    kinds: [{group: b.example, kind: Beta, version: v1}]\n", encoding="utf-8")
    docs = {"one.yaml": _crd_doc("a.example", "Alpha"), "two.yaml": _crd_doc("b.example", "Beta")}

    class State:
        fail: set = set()
        root = crd
        upstream = docs
    state = State()

    def fetch(url):
        name = url.rsplit("/", 1)[1]
        if name.split(".")[0] in state.fail:
            raise OSError("connection refused (test)")
        return name.encode()

    monkeypatch.setattr(MODULE, "REPO_ROOT", tmp_path)  # outputs are named relative to it
    monkeypatch.setattr(MODULE, "CRD_DIR", crd)
    monkeypatch.setattr(MODULE, "SOURCES", crd / "SOURCES.yaml")
    monkeypatch.setattr(MODULE, "LOCK", crd / "GENERATED.json")
    monkeypatch.setattr(MODULE, "_fetch", fetch)
    monkeypatch.setattr(MODULE, "_load_crd_docs", lambda text: [docs[text]])
    monkeypatch.setattr(sys, "argv", ["generate_crd_schemas.py"])
    return state


def _run(capsys):
    try:
        rc = MODULE.main()
    except SystemExit as exc:
        rc = exc.code
    return rc, capsys.readouterr().err


def test_main_vendors_every_source_and_the_lock(offline, capsys):
    rc, err = _run(capsys)
    assert rc == 0, err
    crd = offline.root
    assert json.loads((crd / "a.example" / "Alpha_v1.json").read_text())["description"] == "Alpha schema"
    assert (crd / "b.example" / "Beta_v1.json").read_text().endswith("}\n")
    lock = json.loads((crd / "GENERATED.json").read_text())["sources"]
    assert sorted(lock) == ["one", "two"]


def test_a_later_failing_source_leaves_the_earlier_one_unwritten(offline, capsys):
    """#3981: the schemas are written only after every source was fetched and
    read. The second fetch failing (rc 2) used to leave the first source's
    schema already written and the lock behind it."""
    offline.fail = {"two"}
    rc, err = _run(capsys)
    assert rc == 2 and "two: fetch failed" in err, err
    assert sorted(p.name for p in offline.root.rglob("*") if p.is_file()) == ["SOURCES.yaml"]


@pytest.mark.parametrize("refused", ["fifo", "stdin"])
def test_a_refused_target_is_rc2_one_line_and_nothing_is_written(refused, offline, capsys):
    """#3981: a vendored schema that is a FIFO (the old read of it blocked
    forever) or this process's stdin (refused before it is replaced) stops
    the run with one line naming it, before the first write."""
    import os
    target = offline.root / "b.example" / "Beta_v1.json"
    target.parent.mkdir()
    if refused == "fifo":
        if not hasattr(os, "mkfifo") or not hasattr(__import__("signal"), "SIGALRM"):
            pytest.skip("no FIFO / SIGALRM on this platform")
        os.mkfifo(target)
        import signal

        def hung(signum, frame):
            raise AssertionError("blocked on the FIFO")
        prev = signal.signal(signal.SIGALRM, hung)
        signal.alarm(20)
        try:
            rc, err = _run(capsys)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, prev)
    else:
        target.write_text("{}\n", encoding="utf-8")
        saved = os.dup(0)
        try:
            with open(target, "rb") as fh:
                os.dup2(fh.fileno(), 0)
            rc, err = _run(capsys)
        finally:
            os.dup2(saved, 0)
            os.close(saved)
    assert rc == 2 and str(target) in err, err
    assert len(err.strip().splitlines()) == 1, err
    assert not (offline.root / "a.example").exists()
    assert not (offline.root / "GENERATED.json").exists()


def _tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _change_upstream(offline):
    offline.upstream["one.yaml"]["spec"]["versions"][0]["schema"]["openAPIV3Schema"][
        "description"] = "Alpha schema, changed upstream"


@pytest.mark.parametrize("mode", [[], ["--check"]])
def test_a_lock_that_is_a_fifo_is_rc2_one_line(mode, offline, capsys, monkeypatch):
    """#3981 (reviews): the lock is read before any schema — a FIFO there
    blocked both modes forever (rc 124 under `timeout`)."""
    import os
    import signal
    if not hasattr(os, "mkfifo") or not hasattr(signal, "SIGALRM"):
        pytest.skip("no FIFO / SIGALRM on this platform")
    lock = offline.root / "GENERATED.json"
    os.mkfifo(lock)
    monkeypatch.setattr(sys, "argv", ["generate_crd_schemas.py", *mode])

    def hung(signum, frame):
        raise AssertionError("blocked on the FIFO")
    prev = signal.signal(signal.SIGALRM, hung)
    signal.alarm(20)
    try:
        rc, err = _run(capsys)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)
    assert rc == 2 and str(lock) in err, err
    assert len(err.strip().splitlines()) == 1, err
    assert not (offline.root / "a.example").exists()


def test_a_refused_lock_leaves_the_schemas_unwritten(offline, capsys):
    """#3981 (review): the lock is judged with the schemas before the first
    write. Upstream changed and the lock is this process's stdin: rc 2, one
    line naming it, and neither schema is rewritten."""
    import os
    assert _run(capsys)[0] == 0
    before = _tree(offline.root)
    _change_upstream(offline)
    lock = offline.root / "GENERATED.json"
    saved = os.dup(0)
    try:
        with open(lock, "rb") as fh:
            os.dup2(fh.fileno(), 0)
        rc, err = _run(capsys)
    finally:
        os.dup2(saved, 0)
        os.close(saved)
    assert rc == 2 and str(lock) in err and "standard stream" in err, err
    assert len(err.strip().splitlines()) == 1, err
    assert _tree(offline.root) == before


def test_check_reports_upstream_drift_and_writes_nothing(offline, capsys, monkeypatch):
    """#3981 (review): `--check` is rc 1 on drift and leaves the tree as it
    found it — the writes now sit after the check's return."""
    assert _run(capsys)[0] == 0
    before = _tree(offline.root)
    _change_upstream(offline)
    monkeypatch.setattr(sys, "argv", ["generate_crd_schemas.py", "--check"])
    rc, err = _run(capsys)
    assert rc == 1, err
    assert _tree(offline.root) == before


def test_two_sources_naming_one_output_are_a_caller_error(offline, capsys):
    """#3981 (CodeRabbit): with the writes deferred, which of two sources
    naming one (group, kind, version) wins would depend on the file on disk;
    it is refused instead, before anything is written."""
    (offline.root / "SOURCES.yaml").write_text(
        "sources:\n"
        "  - id: one\n    url: https://example.invalid/v1.0.0/one.yaml\n    ref: v1.0.0\n"
        "    kinds: [{group: a.example, kind: Alpha, version: v1}]\n"
        "  - id: again\n    url: https://example.invalid/v1.0.0/one.yaml\n    ref: v1.0.0\n"
        "    kinds: [{group: a.example, kind: Alpha, version: v1}]\n", encoding="utf-8")
    rc, err = _run(capsys)
    assert rc == 2 and "also produced by source one" in err, err
    assert len(err.strip().splitlines()) == 1, err
    assert sorted(_tree(offline.root)) == ["SOURCES.yaml"]
