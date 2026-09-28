"""「已自動化於 hook: X」這類宣稱，X 必須是真的 pre-commit hook id（#2223）。

WHY: checklist 會在某一步旁邊標 `[已自動化於 hook: X]`，讀者看到就不再自己檢查。
#2223 量到一處標的是 `check_frontmatter_versions`，而 `.pre-commit-config.yaml`
裡根本沒有這個 hook——它其實由 `make lint-docs` 執行。標籤錯了，讀者就會漏跑。

WHAT IS CHECKED: 全 repo 的 Markdown 裡，每個 `已自動化於 hook: a, b + c` 標籤列出的
名稱，都要在 `.pre-commit-config.yaml` 的 hook id 裡找得到。不檢查那支 hook 是否真的
涵蓋了該步驟所說的內容——那需要讀懂散文，不是字串比對能回答的。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from _tree import REPO_ROOT, repo_files

_CLAIM = re.compile(r"已自動化於 hook[:：]\s*([^\]`\n]+)")
_SPLIT = re.compile(r"\s*[,，、+/]\s*")


def _hook_ids() -> set[str]:
    cfg = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    return {hook["id"] for repo in cfg["repos"] for hook in repo["hooks"]}


def _claims() -> list[tuple[Path, int, str]]:
    found = []
    for path in repo_files(".md"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in _CLAIM.finditer(line):
                for name in _SPLIT.split(m.group(1).strip()):
                    if name:
                        found.append((path, lineno, name))
    return found


def test_scan_finds_claims():
    # Non-vacuity: a regex that stopped matching would make the next test pass on nothing.
    assert len(_claims()) >= 3


def test_every_claimed_hook_exists():
    ids = _hook_ids()
    missing = [
        f"{p.relative_to(REPO_ROOT).as_posix()}:{n}: {name}"
        for p, n, name in _claims()
        if name not in ids
    ]
    assert not missing, (
        "these '已自動化於 hook' labels name no hook id in .pre-commit-config.yaml "
        "(fix the label, or say which make target runs it instead):\n  " + "\n  ".join(missing)
    )
