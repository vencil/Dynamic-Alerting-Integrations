#!/usr/bin/env python3
"""`da-tools init` 產物裡兩段給客戶的操作說明，以**照做的結果**守住（#2542）。

WHY THIS FILE EXISTS
--------------------
`scripts/tools/ops/init_project.py` 產出的檔案裡有兩段客戶照著做的說明，都是
#1445 修過的客戶路徑缺陷；改回缺陷原樣後全套測試仍綠（#2542 的 m3／m4d）：

1. `.pre-commit-config.da.yaml` 開頭的合併指示（`_gen_precommit_snippet`）。
   機器標記 `# merge-mode: merge-items` 另有測試在釘，但**給人讀的那段**沒有——
   把它換成「整份 append」，標記照舊，一切照綠。
2. `kustomize/base/README.md` 叫客戶打的那條 `kustomize build …`
   （`_gen_kustomize_base_readme`）。拿掉指令裡的 `--load-restrictor` 後，客戶
   照抄必失敗，但 README 的說明文字仍在，所以既有的字串比對也仍綠。

⛔ 這裡**不比對固定句子**（#1446 停止的正是列舉式寫法）。兩段各自這樣守：

* 合併指示：從產物的註解前言抽出客戶看得到的 code span，**解析成 YAML、對回產物
  本身的節點**——哪一個 span 是「要搬的那一項」（產物某個頂層清單裡的元素），哪
  一個是「要搬進去的清單」（產物的頂層鍵）。解析得出來，才照它在一份既有設定上
  執行合併，斷言原有 hook 與新 hook 都在；解析不出來（例如只剩一句「append this
  file」）就是紅。對照組照「整份 append」在文字層接上去，斷言原有 hook 消失——那
  是警告句所說的事，以行為證明它為真。
* `--load-restrictor`：從 README 的 bash fenced block 抽出 `kustomize build …`
  那一行，在照 README `ln -s` 過的產出樹上**原樣執行**，斷言 rc 0 且產出
  ConfigMap；再把 `--load-restrictor` 那個選項拿掉重跑，斷言失敗——旗標是必要的。

誠實邊界
--------
* 合併指示的判定解析的是**指示的受詞**（搬哪一項、搬到哪），不解析動詞。一段
  同時點名 `- repo: local` 與 `repos:`、動詞卻寫「append」的說明，這裡仍綠。
  code span 是判定的前提：受詞若不再用反引號標出，這裡會紅（寧紅不漏）。
* YAML 以 PyYAML safe loader 解析——pre-commit 讀 `.pre-commit-config.yaml`
  用的是同一個 loader（`pre_commit/yaml.py`），重複鍵同樣「後者勝、不報錯」。
* kustomize 相關測試缺 binary 時 skip，政策與 fixture 沿用
  `tests/ops/test_generated_kustomize_build.py`：CI 的 kustomize step 以
  ``KUSTOMIZE_REQUIRE=1`` 跑**本模組**，那裡缺 binary 是失敗不是 skip；
  該模組的 ``test_ci_runs_this_module_with_the_pinned_kustomize`` 解析 ci.yml
  確認本模組在那一步的 pytest 參數裡。
"""
from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path

import pytest
import yaml

_TESTS_OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS_OPS))

import test_generated_kustomize_build as tkb  # noqa: E402
from test_generated_kustomize_build import (  # noqa: E402
    _assert_built,
    _base,
    _generate,
    _readme,
    _run,
    _setup_symlinks,
    kustomize,  # noqa: F401 — pytest fixture，缺 binary 時 skip／KUSTOMIZE_REQUIRE 時 fail
)

REPO_ROOT = _TESTS_OPS.parents[1]
THIS_MODULE = "tests/ops/test_init_customer_instructions.py"

_posix_only = pytest.mark.skipif(
    os.name == "nt", reason="README 的 setup 是 POSIX `ln -s`")


# ═══════════════════════════════════════════════════════════════════════════
# 1. pre-commit snippet 的合併指示
# ═══════════════════════════════════════════════════════════════════════════
# 客戶既有的 `.pre-commit-config.yaml`：一個常見的第三方 hook。
_EXISTING_CONFIG = """\
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: trailing-whitespace
"""
_EXISTING_HOOK = "trailing-whitespace"

_CODE_SPAN = re.compile(r"`([^`]+)`")


@pytest.fixture(scope="module")
def snippet_text(tmp_path_factory) -> str:
    _root, out = _generate(tmp_path_factory.mktemp("snippet"))
    return (out / ".pre-commit-config.da.yaml").read_text(encoding="utf-8")


def _preamble(text: str) -> str:
    """產物開頭、客戶打開檔案第一眼讀到的註解，接成一段（code span 可跨行）。"""
    lines = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        lines.append(line[1:].strip())
    return " ".join(lines)


def _hook_ids(config: dict) -> set[str]:
    return {h["id"] for repo in config.get("repos") or []
            for h in repo.get("hooks") or []}


def _resolve_merge_instruction(text: str) -> tuple[str, list[dict]]:
    """把前言裡的指示解析成可執行的操作：(目標頂層鍵, 要搬進去的項目)。

    每個 code span 當 YAML 解析，再對回產物本身：
      * 解析成 ``{k: None}`` 且 ``k`` 是產物裡值為清單的頂層鍵 → 目標清單；
      * 解析成單一 mapping 的清單、且該 mapping 是產物某頂層清單裡某項的子集
        → 要搬的那一項（連同它所在的頂層鍵）。
    解析不出「搬哪一項」與「搬進哪個清單」，或兩者對不上，就是指示沒有告訴客戶
    怎麼合併——斷言失敗。
    """
    snippet = yaml.safe_load(text)
    assert isinstance(snippet, dict), snippet
    targets: set[str] = set()
    items: list[tuple[str, dict]] = []
    spans = _CODE_SPAN.findall(_preamble(text))
    for span in spans:
        try:
            node = yaml.safe_load(span)
        except yaml.YAMLError:
            continue
        if (isinstance(node, dict) and len(node) == 1
                and next(iter(node.values())) is None
                and isinstance(snippet.get(next(iter(node))), list)):
            targets.add(next(iter(node)))
        if (isinstance(node, list) and len(node) == 1
                and isinstance(node[0], dict) and node[0]):
            for key, seq in snippet.items():
                for it in seq if isinstance(seq, list) else ():
                    if (isinstance(it, dict)
                            and all(it.get(k) == v for k, v in node[0].items())
                            and (key, it) not in items):
                        items.append((key, it))
    assert targets and items, (
        "the snippet's preamble no longer tells the customer WHICH item to move "
        "and INTO WHICH list: no code span resolves to a list item of the "
        f"snippet and one to its top-level list key (spans: {spans})")
    assert len(targets) == 1, f"ambiguous merge target: {sorted(targets)}"
    (target,) = targets
    assert all(key == target for key, _ in items), (
        f"the item(s) named live under {[k for k, _ in items]}, not under the "
        f"named target `{target}:`")
    moved = [it for _, it in items]
    assert len(moved) == len(snippet[target]), (
        f"following the instruction moves {len(moved)} of the "
        f"{len(snippet[target])} item(s) in `{target}:` — the rest would be lost")
    return target, moved


def test_following_the_merge_instruction_keeps_every_hook(snippet_text):
    """照產物前言說的做法合併進既有設定：原有 hook 與產物的 hook 都在。"""
    target, moved = _resolve_merge_instruction(snippet_text)
    existing = yaml.safe_load(_EXISTING_CONFIG)
    existing[target].extend(moved)
    merged = yaml.safe_load(yaml.safe_dump(existing, sort_keys=False))
    expected = {_EXISTING_HOOK} | _hook_ids(yaml.safe_load(snippet_text))
    assert _hook_ids(merged) == expected, (
        f"after the instructed merge: {sorted(_hook_ids(merged))}, "
        f"expected {sorted(expected)}")


def test_appending_the_file_whole_silently_drops_existing_hooks(snippet_text):
    """對照組：整份文字接在既有設定後面——YAML 重複頂層鍵，後者勝，原有 hook 消失。

    這是產物警告客戶不要做的事；這條證明那個警告在今天的產物上仍是真的，也證明
    上面那條的合併操作不是「怎麼接都行」。
    """
    appended = yaml.safe_load(_EXISTING_CONFIG + "\n" + snippet_text)
    ids = _hook_ids(appended)
    assert _EXISTING_HOOK not in ids, (
        "appending the snippet whole now KEEPS the existing hook — the "
        "snippet's shape changed, so its do-not-append warning (and the "
        "merge-mode marker) need re-deriving")
    assert ids == _hook_ids(yaml.safe_load(snippet_text)), ids


# ═══════════════════════════════════════════════════════════════════════════
# 2. kustomize/base/README.md 叫客戶打的 `kustomize build`
# ═══════════════════════════════════════════════════════════════════════════
_BASH_FENCE = re.compile(r"^```bash\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _readme_build_command(out: Path) -> list[str]:
    """README bash fenced block 裡的那一條 `kustomize build …`（argv）。"""
    cmds = [shlex.split(line)
            for block in _BASH_FENCE.findall(_readme(out))
            for line in block.splitlines()
            if line.split()[:2] == ["kustomize", "build"]]
    assert len(cmds) == 1, (
        "expected exactly one `kustomize build …` line in a ```bash block of "
        f"kustomize/base/README.md, found {cmds}")
    return cmds[0]


def _without_load_restrictor(argv: list[str]) -> list[str]:
    stripped: list[str] = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
        elif arg == "--load-restrictor":
            skip = True
        elif not arg.startswith("--load-restrictor="):
            stripped.append(arg)
    return stripped


@_posix_only
def test_readme_build_command_builds_the_tree_as_written(kustomize, tmp_path):  # noqa: F811
    """README 的 `ln -s` setup → README 的 `kustomize build` 原樣執行 → rc 0＋ConfigMap。"""
    _root, out = _generate(tmp_path)
    _setup_symlinks(out)
    argv = _readme_build_command(out)
    _assert_built(_run([kustomize, *argv[1:]], out), out)


@_posix_only
def test_readme_build_command_needs_its_load_restrictor(kustomize, tmp_path):  # noqa: F811
    """同一條 README 指令拿掉 `--load-restrictor` 就失敗：旗標是必要的，不是裝飾。"""
    _root, out = _generate(tmp_path)
    _setup_symlinks(out)
    argv = _readme_build_command(out)
    bare = _without_load_restrictor(argv)
    assert bare != argv, (
        f"the README's build command {argv} passes no --load-restrictor, yet "
        "the README's own setup links conf.d/ files from outside the base")
    proc = _run([kustomize, *bare[1:]], out)
    assert proc.returncode != 0, (
        "the README build command succeeds WITHOUT --load-restrictor over the "
        "README's symlink setup — the flag (and its ⛔ section) went stale")
    assert any(p.is_symlink() for p in _base(out).iterdir()), (
        "the README setup linked nothing — this failure says nothing about the flag")


def test_this_module_is_covered_by_the_kustomize_ci_guard():
    """本模組的 kustomize 測試靠 CI 的 KUSTOMIZE_REQUIRE step 才不會 skip-as-green；
    那道守衛逐一檢查 ``tkb.KUSTOMIZE_MODULES``，所以本模組必須在清單裡、路徑也要對。"""
    assert (REPO_ROOT / THIS_MODULE).resolve() == Path(__file__).resolve()
    assert THIS_MODULE in tkb.KUSTOMIZE_MODULES, tkb.KUSTOMIZE_MODULES
