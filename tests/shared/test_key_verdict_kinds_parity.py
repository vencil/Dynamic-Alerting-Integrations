"""共用判決的 finding kind：Go 定義一次，Python 讀者的封閉清單要跟它一樣（#3591）。

kind 在 `pkg/keyverdict/keyverdict.go` 定義（`Kind<名> = "<值>"`）；
`_lib_key_verdict._KINDS` 是讀判決文件時的封閉集合（不認得的 kind 當成讀不懂的輸出），
路由產生器的 `_KEY_VERDICT_KINDS` 把它們對到 `--findings-json` 的 kind，
`_grar_merge.FINDING_KINDS` 再為每個 kind 寫說明。新增一個 kind 卻漏掉其中一處，
前兩者會讓 CI 的讀者在第一次遇到它時整份判決讀不懂；這支測試讓漏掉的那一處在合入前就紅。

讀 Go 原始碼而不是另放清單：找不到常數區塊、或一個常數都沒讀到，都算失敗——讀不到不能當成一致。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))

import _lib_key_verdict  # noqa: E402
from _grar_merge import FINDING_KINDS  # noqa: E402
from generate_alertmanager_routes import _KEY_VERDICT_KINDS  # noqa: E402

KEYVERDICT_GO = (REPO / "components" / "threshold-exporter" / "app" / "pkg" / "keyverdict"
                 / "keyverdict.go")


def _go_kinds() -> set[str]:
    src = KEYVERDICT_GO.read_text(encoding="utf-8")
    m = re.search(r"^// Finding kinds\.\nconst \((.*?)^\)\n", src, re.S | re.M)
    assert m, f"the `// Finding kinds.` const block is not in {KEYVERDICT_GO}"
    kinds = set(re.findall(r"^\s*Kind\w+\s*=\s*\"([a-z_]+)\"", m.group(1), re.M))
    assert kinds, f"no Kind constant read from {KEYVERDICT_GO}"
    return kinds


def test_python_reader_takes_exactly_the_go_kinds():
    assert set(_lib_key_verdict._KINDS) == _go_kinds()


def test_route_generator_maps_every_kind():
    assert set(_KEY_VERDICT_KINDS) == _go_kinds()
    missing = sorted(set(_KEY_VERDICT_KINDS.values()) - set(FINDING_KINDS))
    assert not missing, f"FINDING_KINDS has no entry for {missing}"


# tenant-api 讀判決時自己有一份 kind 清單（`internal/gitops/key_verdict.go`）：
# `verdictKinds` 是每個 kind 的說明，`keyValidationKinds` 是「這次寫入的 KeyValidation
# 已經用 ratchet 判過、判決不再重擋」的那幾個。新增 kind 卻沒在 tenant-api 決定它屬不屬
# `keyValidationKinds`，tenant-api 會把磁碟上原封不動的值當成這次寫入的新錯誤拒收（#3834）。
# 要求 `verdictKinds` 剛好等於 Go 的 kind，新增 kind 就一定得打開這個檔案做那個決定。
TENANT_API_GO = REPO / "components" / "tenant-api" / "internal" / "gitops" / "key_verdict.go"


def _tenant_api_map_kinds(src: str, decl: str) -> set[str]:
    """`decl` 這個 map 字面值的鍵：字串字面值，或本檔 `const Kind… = "…"` 的名字。"""
    consts = dict(re.findall(r'^const (Kind\w+) = "([a-z_]+)"$', src, re.M))
    m = re.search(rf"^var {decl} = map\[string\]\w+\{{(.*?)\}}\n", src, re.S | re.M)
    assert m, f"`var {decl} = map[string]…{{` is not in {TENANT_API_GO}"
    body = re.sub(r"//[^\n]*", "", m.group(1))
    keys = re.findall(r'(?:"([a-z_]+)"|\b(Kind\w+))\s*:', body)
    assert keys, f"no key read from {decl} in {TENANT_API_GO}"
    out = set()
    for lit, name in keys:
        if name:
            assert name in consts, f"{decl} names {name}, which is no `const {name} = \"…\"` there"
            out.add(consts[name])
        else:
            out.add(lit)
    # 每個 `鍵: 值` 都要被讀到：pattern 漏掉的就是這支測試悄悄沒比的一項。
    # 字串字面值先清空，冒號才只剩鍵值之間的那一個。
    assert re.sub(r'"[^"]*"', '""', body).count(":") == len(keys), body
    return out


def test_tenant_api_describes_exactly_the_go_kinds():
    src = TENANT_API_GO.read_text(encoding="utf-8")
    assert _tenant_api_map_kinds(src, "verdictKinds") == _go_kinds()


def test_tenant_api_key_validation_kinds_are_verdict_kinds():
    src = TENANT_API_GO.read_text(encoding="utf-8")
    assert _tenant_api_map_kinds(src, "keyValidationKinds") <= _go_kinds()
