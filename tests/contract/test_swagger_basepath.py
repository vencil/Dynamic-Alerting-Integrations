"""tenant-api OpenAPI 2.0 產物的 basePath／paths 組合防回歸（#2597）。

OpenAPI 2.0 規定 ``paths`` 的鍵相對於 ``basePath``：實際路徑是
``basePath + path``。tenant-api 的 ``@Router`` 註解寫的是完整路徑
（``/api/v1/views``），若 ``cmd/server/main.go`` 同時宣告
``@BasePath /api/v1``，依規格組出來就是 ``/api/v1/api/v1/views``——
照規格產 client 或接 gateway 的人會打到不存在的路徑。

schemathesis 契約測試看不出這件事：帶 ``--url`` 時它完全不用 spec 的
basePath（base path 取自 ``--url`` 本身），所以這裡以結構解析產物直接守。
"""
from __future__ import annotations

import json
import posixpath
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "components" / "tenant-api" / "docs"

# tenant-api 的路由實際掛載點（cmd/server/routes.go）。
MOUNT_PREFIX = "/api/v1/"


def _load_json() -> dict:
    return json.loads((DOCS_DIR / "swagger.json").read_text(encoding="utf-8"))


def _load_yaml() -> dict:
    return yaml.safe_load((DOCS_DIR / "swagger.yaml").read_text(encoding="utf-8"))


def _segments(path: str) -> list[str]:
    return [seg for seg in path.split("/") if seg]


def _effective_path(base_path: str, path: str) -> str:
    """依 OpenAPI 2.0 把 basePath 與 path 組成實際請求路徑。"""
    return posixpath.join("/", base_path.lstrip("/"), path.lstrip("/"))


@pytest.mark.parametrize("loader", [_load_json, _load_yaml], ids=["json", "yaml"])
def test_base_path_is_not_repeated_in_paths(loader) -> None:
    spec = loader()
    assert spec.get("swagger") == "2.0", "本測試的組合規則只適用 OpenAPI 2.0"

    paths = spec.get("paths") or {}
    assert paths, "spec 沒有任何 path——斷言會空轉，先確認產物"

    base_path = spec.get("basePath", "/")
    base_segs = _segments(base_path)

    doubled = []
    unmounted = []
    for path in paths:
        effective = _effective_path(base_path, path)
        if base_segs and _segments(path)[: len(base_segs)] == base_segs:
            doubled.append(f"{base_path} + {path} -> {effective}")
        if not effective.startswith(MOUNT_PREFIX):
            unmounted.append(f"{base_path} + {path} -> {effective}")

    assert not doubled, (
        "basePath 與 path 前綴重複，依規格會組出重複前綴的路徑：\n  "
        + "\n  ".join(doubled)
    )
    assert not unmounted, (
        f"組出的實際路徑不在 {MOUNT_PREFIX} 之下：\n  " + "\n  ".join(unmounted)
    )
