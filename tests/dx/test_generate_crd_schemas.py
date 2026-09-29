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
    """The ONLY guard on `_load_crd_docs`'s safety: it drives a SafeLoader
    subclass by hand, which is outside bandit B506's predicate. The control
    beside it is `test_the_crd_reader_still_accepts_the_value_tag`."""
    yaml = pytest.importorskip("yaml")
    payload = "!!python/object/apply:os.system ['true']\n"
    with pytest.raises(yaml.constructor.ConstructorError):
        list(MODULE._load_crd_docs(payload))


def test_importing_the_tool_leaves_the_global_safe_loader_alone() -> None:
    """Run in a FRESH interpreter: this pytest worker has already imported the
    module (MODULE above), so an in-process check would pass or fail depending
    on what else ran first. The `before` row is the anti-vacuity half — if `=`
    were already accepted before the import, the `after` row proves nothing."""
    repo = TOOL.parents[3]
    script = (
        "import json, sys, yaml\n"
        "sys.path[:0] = [sys.argv[1], sys.argv[2]]\n"
        "import _lib_yaml_keys as K, _lib_io as IO\n"
        "loaders = {\n"
        "    'safe_load': lambda s: yaml.safe_load(s),\n"
        "    'load_exporter_keys': lambda s: K.load_exporter_keys(s),\n"
        "    'StrictExporterKeyLoader':\n"
        "        lambda s: yaml.load(s, Loader=IO.StrictExporterKeyLoader),\n"
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
