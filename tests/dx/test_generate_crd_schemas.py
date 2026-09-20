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
"""

import importlib.util
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
