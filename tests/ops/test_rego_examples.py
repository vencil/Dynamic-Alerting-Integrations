"""文件裡的 rego 範例在真的 OPA 上求值，結果與預期相同（#4110）。

`docs/cli-reference.md` 與 `.en.md` 的 opa-evaluate 一節以 `<!-- rego-example: <名稱> -->`
標記 fenced rego 區塊。這裡：

* 兩份文件的範例名稱集合相同、同名範例逐字相同（雙語漂移）；
* 每個範例在 `tests/fixtures/opa/<名稱>/` 有一棵樹與 `expected.json`，每個 fixture 也有範例；
* 對那棵樹跑 `opa-evaluate --dry-run --at <expected.at>` 得到 input，以 `opa eval` 求
  `data.<package>.violations`，`(tenant, field, severity)` 的集合等於 `expected.json`。

求值需要 OPA：`$OPA_BINARY`（版本須等於 `tests/fixtures/opa/opa.env` 的
`OPA_VERSION`）。沒有時跳過——除非 `OPA_REQUIRED=1`，那時判紅：CI 的 Lint Documentation
job 裝好釘版的 OPA 並設這個變數，所以那裡的「沒跑」不會讀成「過了」。Claude Code 的
session 不允許執行 OPA binary，求值只在 CI 上跑。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from _tree import repo_files  # noqa: E402  (tests/ on sys.path: conftest)

ROOT = Path(__file__).resolve().parents[2]
DOCS = (ROOT / "docs" / "cli-reference.md", ROOT / "docs" / "cli-reference.en.md")
FIXTURES = ROOT / "tests" / "fixtures" / "opa"
BRIDGE = ROOT / "scripts" / "tools" / "ops" / "policy_opa_bridge.py"

_MARKER = re.compile(r"<!-- rego-example: ([a-z0-9-]+) -->\n```rego\n(.*?)\n```", re.S)
#: A fenced block of Rego by any spelling of its info string, and the line before it.
_FENCE = re.compile(r"^(.*)\n```[ \t]*(rego|opa)\b", re.M | re.I)
_ANY_MARKER = re.compile(r"rego-example:", re.I)


def _examples(doc: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for name, code in _MARKER.findall(doc.read_text(encoding="utf-8")):
        assert name not in found, f"{doc.name}: rego example {name!r} twice"
        found[name] = code
    return found


def _pin() -> dict[str, str]:
    out = {}
    for line in (FIXTURES / "opa.env").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out[key] = value
    return out


ZH, EN = (_examples(d) for d in DOCS)
FIXTURE_NAMES = sorted(p.name for p in FIXTURES.iterdir() if p.is_dir())


def test_the_examples_are_the_same_in_both_languages():
    assert ZH, "no marked rego example in docs/cli-reference.md"
    assert ZH == EN, {n: (ZH.get(n) == EN.get(n)) for n in sorted(set(ZH) | set(EN))}


def test_every_example_has_a_fixture_and_every_fixture_an_example():
    assert sorted(ZH) == FIXTURE_NAMES
    for name in FIXTURE_NAMES:
        assert (FIXTURES / name / "conf.d").is_dir() and (FIXTURES / name / "expected.json").is_file(), name


def test_every_rego_block_in_the_docs_is_a_checked_example():
    """A Rego block anywhere under docs/ is one of the examples evaluated here:
    marked, spelt ```rego, and in the two pages this test reads — never an
    unchecked copy beside them."""
    unchecked = []
    for doc in repo_files(".md"):
        rel = doc.relative_to(ROOT).as_posix()
        if not rel.startswith("docs/"):
            continue
        text = doc.read_text(encoding="utf-8")
        for m in _FENCE.finditer(text):
            line = text[:m.start()].count("\n") + 2
            if doc not in DOCS or not re.fullmatch(r"<!-- rego-example: [a-z0-9-]+ -->", m.group(1)) \
                    or m.group(2) != "rego":
                unchecked.append(f"{rel}:{line}")
        if doc not in DOCS and _ANY_MARKER.search(text):
            unchecked.append(f"{rel}: a rego-example marker outside {[d.name for d in DOCS]}")
    assert not unchecked, unchecked


def test_the_pin_is_one_version_and_one_checksum():
    pin = _pin()
    assert set(pin) == {"OPA_VERSION", "OPA_SHA256"}, pin
    assert re.fullmatch(r"v\d+\.\d+\.\d+", pin["OPA_VERSION"]), pin
    assert re.fullmatch(r"[0-9a-f]{64}", pin["OPA_SHA256"]), pin


def _opa() -> str:
    binary = os.environ.get("OPA_BINARY", "")
    if not binary:
        if os.environ.get("OPA_REQUIRED") == "1":
            pytest.fail("OPA_REQUIRED=1 but OPA_BINARY is not set: the examples would go unevaluated")
        pytest.skip("OPA_BINARY is not set: the rego examples are evaluated in CI (Lint Documentation)")
    version = subprocess.run([binary, "version"], capture_output=True, text=True, encoding="utf-8",
                             timeout=60, check=True).stdout
    want = _pin()["OPA_VERSION"].removeprefix("v")
    assert f"Version: {want}\n" in version, (version, want)
    return binary


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_the_example_evaluates_to_its_expected_violations(name, tmp_path):
    opa = _opa()
    expected = json.loads((FIXTURES / name / "expected.json").read_text(encoding="utf-8"))
    code = ZH[name]
    package = re.search(r"^package ([\w.]+)$", code, re.M)
    assert package, f"{name}: no package line"
    dry = subprocess.run(
        [sys.executable, "-s", str(BRIDGE), "--config-dir", str(FIXTURES / name / "conf.d"),
         "--dry-run", "--at", expected["at"]],
        capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert dry.returncode == 0, dry.stderr
    (tmp_path / "input.json").write_text(dry.stdout, encoding="utf-8")
    (tmp_path / "example.rego").write_text(code + "\n", encoding="utf-8")
    run = subprocess.run(
        [opa, "eval", "--format", "json", "--data", str(tmp_path / "example.rego"),
         "--input", str(tmp_path / "input.json"), f"data.{package.group(1)}.violations"],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)["result"]
    got = result[0]["expressions"][0]["value"] if result else None
    assert isinstance(got, list), run.stdout
    assert sorted((v["tenant"], v["field"], v["severity"]) for v in got) == \
        sorted((v["tenant"], v["field"], v["severity"]) for v in expected["violations"]), got
