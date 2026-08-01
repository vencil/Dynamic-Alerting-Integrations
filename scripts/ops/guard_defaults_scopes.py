"""解析改動的 `_defaults.yaml` 清單 → da-guard 該用哪些 (config-dir, scope) 跑。

存在理由（#1219 / TRK-345）
--------------------------
`guard-defaults-impact.yml` 原本假設「所有 `_defaults.yaml` 都住在同一個
conf.d 根底下」，於是把 scope 算成單一目錄、config-dir 固定成偵測到的那個根。
實際上本 repo 的 `_defaults.yaml` 橫跨多個彼此獨立的 conf.d 樹（出貨設定、
try-local seed、recipe 範例、e2e-bench fixture、golden fixture），
**其中只有一個**在預設偵測到的根底下。加上 shallow clone 讓 `git diff` 取不到
base ref、失敗又被 `|| true` 吞掉，scope 於是靜默退回一個 PR 從沒碰過的目錄，
guard 驗錯樹之後貼上綠色 sticky comment。

把「哪個檔屬於哪棵樹」這件事從 workflow 的 inline bash 搬到這裡，理由有二：
往上找根的邏輯在 bash 裡難讀也難測；而這正是先前失效的那一步，它應該有
單元測試釘住（`tests/ops/test_guard_defaults_scopes.py`）。

規則
----
* 一個檔案的 **conf.d 根** ＝ 往上找到的第一個名為 `conf.d` 的祖先目錄。
* 同一個根底下若只動到單一目錄 → scope 收窄到該目錄；動到多個 → scope 用根本身
  （沿用原 workflow 對「多檔串接編輯」的處置：整棵重驗）。
* 找不到 `conf.d` 祖先的檔案 ＝ **unmanaged**，由呼叫端顯式回報「未檢查、原因為何」。
  ⛔ 不可靜默略過——那正是本票要消滅的失效形狀。

輸出（TSV，供 workflow 的 bash 直接讀）
--------------------------------------
  target<TAB><config-dir><TAB><scope>
  unmanaged<TAB><path>

用法
----
  python3 scripts/ops/guard_defaults_scopes.py <changed-path>...
  git diff --name-only ... | python3 scripts/ops/guard_defaults_scopes.py -
"""
from __future__ import annotations

import sys
from pathlib import PurePosixPath

CONF_D = "conf.d"


def conf_d_root(path: str) -> str | None:
    """回傳 `path` 所屬的 conf.d 根（POSIX 相對路徑），找不到則 None。

    以最近的祖先為準：巢狀 conf.d（`a/conf.d/b/conf.d/x.yaml`）取內層那個，
    因為內層才是那份設定實際被解析時的根。
    """
    p = PurePosixPath(path.replace("\\", "/"))
    for parent in p.parents:
        if parent.name == CONF_D:
            return str(parent)
    return None


def resolve(paths: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """(targets, unmanaged)；targets 為 (config_dir, scope)，依 config_dir 排序。"""
    by_root: dict[str, set[str]] = {}
    unmanaged: list[str] = []
    for raw in paths:
        path = raw.strip()
        if not path:
            continue
        root = conf_d_root(path)
        if root is None:
            unmanaged.append(path)
            continue
        parent = str(PurePosixPath(path.replace("\\", "/")).parent)
        by_root.setdefault(root, set()).add(parent)

    targets: list[tuple[str, str]] = []
    for root in sorted(by_root):
        dirs = by_root[root]
        # 單一目錄才收窄；多個目錄代表串接編輯，整棵重驗才驗得出跨層的
        # redundant-override（沿用原 workflow 的語意）。
        scope = next(iter(dirs)) if len(dirs) == 1 else root
        targets.append((root, scope))
    return targets, sorted(unmanaged)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["-"] or not args:
        paths = sys.stdin.read().splitlines()
    else:
        paths = args

    targets, unmanaged = resolve(paths)
    out = sys.stdout
    for config_dir, scope in targets:
        out.write(f"target\t{config_dir}\t{scope}\n")
    for path in unmanaged:
        out.write(f"unmanaged\t{path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
