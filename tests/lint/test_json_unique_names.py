"""repo 裡每份 JSON 的每個 object，同一個名字只出現一次（#4102）。

為什麼
------
RFC 8259 把重複名字的處理留給讀者，而 Python 的 ``json.loads`` 與 Go 的
``encoding/json`` 都是「留最後一個、一聲不吭」。``tenant-config.schema.json``
的 ``metadata`` 曾經同時有兩段 ``$comment``：任何讀者都只看得到後一段，前一段
（ADR-016 的路徑推導說明）等於不存在，而沒有任何東西會說出來。

工具讀**呼叫端**給的 JSON 早就拒收重複名（#4064，``_lib_input.decode_bytes``
的 ``object_pairs_hook``）；本檔把同一個判定套到 repo **自己**的 JSON 上，
所以兩邊的「重複」是同一個定義，不是各寫一份。

不是嚴格 JSON 的檔案列在 ``NOT_STRICT_JSON``，每一筆附它實際的格式：

* JSON Lines：逐行以同一個判定解，每一行仍不准有重複名。
* JSONC（允許註解）：stdlib 解不了，整份略過，並在理由裡寫明。

清單只會變短：一筆列在裡面卻已經能當嚴格 JSON 解，或檔案已不存在，都轉紅，
免得一份檔案被永久豁免、在它變回普通 JSON 之後仍然沒人檢查。

列舉走 ``tests/_tree.py`` 的 ``repo_files``（``git ls-files``），不會把
``.claude/worktrees`` 裡的 repo 副本當成本樹。
"""
from __future__ import annotations

import json

import pytest

from _lib_input import DocumentError, decode_bytes
from _tree import REPO_ROOT, repo_files

# repo 相對路徑 → (格式, 理由)。格式只有兩種：
#   "jsonl" — 每一行是一份 JSON，逐行套同一個判定
#   "jsonc" — 帶註解的 JSON，stdlib 解不了，整份略過
NOT_STRICT_JSON: dict[str, tuple[str, str]] = {
    ".devcontainer/devcontainer.json": (
        "jsonc", "Dev Container spec is JSON with comments; stdlib json cannot read it"),
    "tests/lint/fixtures/trufflehog/findings_mixed.json": (
        "jsonl", "trufflehog --json prints one finding per line"),
    "tests/lint/fixtures/trufflehog/findings_unverified_only.json": (
        "jsonl", "trufflehog --json prints one finding per line"),
}


def _rel(path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _decode(raw: bytes, name: str):
    return decode_bytes(raw, fmt="json", name=name)


def _json_files() -> list:
    files = repo_files(".json")
    # fail-closed：列舉落空不能讀成「沒有違例」。
    assert files, "repo_files('.json') found no JSON file; the scan ran on the wrong tree"
    return files


def test_every_json_object_names_each_member_once() -> None:
    bad: list[str] = []
    for path in _json_files():
        rel = _rel(path)
        kind = NOT_STRICT_JSON.get(rel, ("json", ""))[0]
        if kind == "jsonc":
            continue
        raw = path.read_bytes()
        chunks = ([(n, line) for n, line in enumerate(raw.splitlines(), 1) if line.strip()]
                  if kind == "jsonl" else [(None, raw)])
        for lineno, chunk in chunks:
            where = rel if lineno is None else f"{rel}:{lineno}"
            try:
                _decode(chunk, where)
            except DocumentError as exc:
                bad.append(f"{where}: {exc}")
    assert not bad, (
        f"{len(bad)} JSON file(s) do not decode as strict JSON with each name given once "
        "per object (#4102). A repeated name keeps only one of its values in Python's "
        "json and Go's encoding/json, silently; merge the two members into one. A file "
        "that is not plain JSON belongs in NOT_STRICT_JSON with its real format:\n  " + "\n  ".join(bad))


def _is_plain_json(raw: bytes) -> bool:
    """Whether *raw* is one JSON document by syntax alone. Repeated names are
    deliberately NOT looked at here: an exemption is about the format, so a
    file that parses as plain JSON no longer needs one even if it repeats a
    name — and dropping the exemption is exactly what lets the scan above
    see that repeat."""
    try:
        json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return True


@pytest.mark.parametrize("rel", sorted(NOT_STRICT_JSON))
def test_each_exemption_is_still_needed(rel: str) -> None:
    path = REPO_ROOT / rel
    assert path.is_file(), f"{rel} is listed in NOT_STRICT_JSON but no longer exists; drop it"
    assert not _is_plain_json(path.read_bytes()), (
        f"{rel} now parses as plain JSON; drop it from NOT_STRICT_JSON so it is checked "
        "like every other JSON file")


@pytest.mark.parametrize("text, plain", [
    ('{"name": "a", "name": "b"}', True),    # a repeat is not a reason to stay exempt
    ('{"a": 1}\n{"a": 2}\n', False),          # JSON Lines
    ('{\n  // a comment\n  "a": 1\n}', False),  # JSONC
])
def test_the_exemption_check_judges_syntax_not_names(text: str, plain: bool) -> None:
    assert _is_plain_json(text.encode("utf-8")) is plain


@pytest.mark.parametrize("text", [
    '{"a": 1, "a": 2}',
    '{"outer": {"$comment": "x", "$comment": "y"}}',
    '[{"k": 1}, {"k": 1, "k": 1}]',
])
def test_the_shared_decoder_refuses_a_repeated_name(text: str) -> None:
    # 守的是判定本身還活著：decode_bytes 若不再掛 object_pairs_hook，
    # 上面那格會對一個真的有重複名的檔案也回綠。
    with pytest.raises(DocumentError, match="given twice"):
        _decode(text.encode("utf-8"), "<sample>")


def test_the_same_name_in_different_objects_is_fine() -> None:
    assert _decode(b'{"a": {"k": 1}, "b": {"k": 2}}', "<sample>") == {
        "a": {"k": 1}, "b": {"k": 2}}
