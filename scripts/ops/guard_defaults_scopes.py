"""解析改動的 `_defaults.yaml` 清單 → da-guard 該用哪些 (config-dir, scope) 跑。

存在理由（#1219 / TRK-345）
--------------------------
`guard-defaults-impact.yml` 原本假設「所有 `_defaults.yaml` 都住在同一個
conf.d 根底下」，於是把 scope 算成單一目錄、config-dir 固定成偵測到的那個根。
實際上本 repo 的 `_defaults.yaml` 橫跨多個彼此獨立的 conf.d 樹（出貨設定、
try-local seed、recipe 範例、e2e-bench fixture；golden fixture 刻意不驗，見下），
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
* scope **相對於 config-dir** 輸出（`db`；根本身為 `.`），因為 da-guard 的
  `--scope` 相對路徑是對 `--config-dir` 解析（#2588）。過去輸出 repo 相對路徑
  （`conf.d/db`），那是 da-guard 以前對工作目錄解析時的寫法；現在照舊傳會被解析成
  `conf.d/conf.d/db`。
* 找不到 `conf.d` 祖先的檔案 ＝ **unmanaged**，由呼叫端顯式回報「未檢查、原因為何」。
  ⛔ 不可靜默略過——那正是本票要消滅的失效形狀。
* 落在 `EXCLUDED_PREFIXES` 底下的檔案 ＝ **excluded**，不交給 da-guard、也不併進任何
  target，同樣由呼叫端點名回報（原因欄是這裡給的那句）。目前只有
  `tests/golden/fixtures/`：那些樹**刻意**寫壞（root `defaults:` 放 mapping、字串），
  與 `.pre-commit-config.yaml` 的 schema 檢查排除它的理由相同。每棵樹的
  `da-guard served-values` 結束碼由 `tests/golden/not_served.json` 與
  `cmd/da-guard/golden_served_test.go` 釘住；主檢查（`da-guard --config-dir`）在這些
  樹上的結果則**刻意不判**（`mixed-mode` 回 1、`wrapper-siblings` 回 4，哪裡都沒釘）。
  ⛔ 前綴只在這裡定義一次；workflow 只認 row kind。

輸出（TSV，供 workflow 的 bash 直接讀）
--------------------------------------
  target<TAB><config-dir><TAB><scope（相對 config-dir）>
  unmanaged<TAB><path>
  excluded<TAB><path><TAB><原因>
  removed<TAB><config-dir>
  nested<TAB><內層 conf.d><TAB><外層 conf.d>

`removed`（#3644）＝這個 PR 把整棵樹刪掉了，沒有東西可驗。兩個條件**都**成立才給：

1. 這棵樹在變更清單裡的每一個路徑，都出現在 `--deleted` 清單（`git diff -z
   --name-only --diff-filter=D` 的輸出）裡；
2. 這棵樹的根目錄在工作目錄（＝HEAD 的 checkout）裡已經不存在。

⛔ 不能只看條件 2。「目錄不存在」也是路徑被改寫過（見下方非 ASCII 一節）的症狀，
那種情況必須維持 `target` ⇒ da-guard exit 2 ⇒ 紅燈；只看目錄會把它洗成「整棵刪除」
的綠燈。條件 1 把兩者分開：被改寫的路徑不會逐字出現在 git 自己列出的刪除清單裡。
⚠️ 沒給 `--deleted` ＝ 沒有刪除資訊 ＝ 永遠不給 `removed`，維持舊行為（紅燈）；
失敗方向是「多驗一次而紅」，不是「少驗而綠」。

⛔ 巢狀 conf.d 不支援（#3694 後續）。`a/conf.d/b/conf.d/` 有兩種讀法：以內層為
`--config-dir`，或以外層為根——exporter 的 walker 走進所有非隱藏子目錄、對名為
`conf.d` 的目錄沒有特例，所以內層的檔也是外層的檔，而外層的 `_defaults` 會往下套。
兩種部署得到的有效設定不同，從目錄名稱推不出哪一種才是真的根；選任一種驗，另一種
就是「被改了卻沒被驗」的綠燈。因此一律判 `nested`（紅），請把佈局攤平：

  nested<TAB><內層 conf.d><TAB><外層 conf.d>

判定一律看 HEAD 的 checkout（有沒有兩層都存在），兩個來源：
* 一個變更路徑往上**仍存在**的 conf.d 祖先有兩層以上；
* 任一個 target 的根目錄底下，在 checkout 裡還有另一個（非隱藏的）`conf.d` 目錄
  ——只改外層的檔也一樣，外層樹的有效設定就含著內層。
「含著」照 exporter 的 walker 判：它不進 `.` 開頭的目錄、不跟隨目錄 symlink
（#1972），所以藏在那兩種路徑後面的 conf.d 不算巢狀，各自是自己的根。

例外是 PR 把內層整棵刪掉、外層還在：PR 之後已經不巢狀了，而外層樹被改了（內層的檔
原本屬於它），所以外層列為 `target`，scope 取被刪子樹往上第一個仍存在的目錄；被刪
的那棵若符合上面兩個條件，照樣輸出 `removed`。外層也整棵刪掉時兩棵都是 `removed`。

用法
----
  python3 scripts/ops/guard_defaults_scopes.py <changed-path>...
  git diff -z --name-only ... | python3 scripts/ops/guard_defaults_scopes.py --null -
  ... | python3 scripts/ops/guard_defaults_scopes.py --null --deleted <NUL 分隔的刪除清單檔> -

⛔ 非 ASCII 路徑必須走 `-z` + `--null`
------------------------------------
`git diff --name-only` 預設（`core.quotePath=true`）會把非 ASCII 路徑整個包進雙
引號並改寫成反斜線八進位跳脫（`"\347\222\260-prod/conf.d/_defaults.yaml"`）。
這支腳本**不解碼那個格式**，而且不能解碼——編碼過的路徑會落到兩種都錯的結果
（皆為實測）：

* 根在 `conf.d` 之上 → `target`，config_dir 變成一個不存在的目錄
  ⇒ da-guard exit 2 ⇒ job 紅，而訊息指著 da-guard 與一個不存在的路徑；
* 根在 `conf.d` 之下 → `unmanaged` ⇒ WORST 維持 0 ⇒ **job 綠**，
  留言還斷言「此檔不在任何 conf.d 樹底下」——而它就在底下。

⚠️ 這一段先前把成因寫成「`path.replace("\\", "/")` 會把跳脫序列變成路徑分隔符」。
那個 `replace` 曾經存在，而它本身就是缺陷：反斜線在 POSIX 檔名裡**合法**，於是一個
真的叫 `we\\ird` 的目錄被改寫成 `we/ird`，scope 指向一個不存在的目錄、da-guard exit 2
⇒ **紅燈指著一個從沒出現在 PR diff 裡的路徑**（實測）。輸入永遠來自 Linux 的
`git diff -z`，分隔符只可能是 `/`，所以那個正規化沒有任何合法輸入，已移除；
上面那兩種壞結果**不需要**它也照樣成立，成因是編碼本身。

正解不是在這裡加一個反跳脫器（那是把判斷做在已經被抹平的那一層），而是**要求
上游根本不要編碼**：`-z` 讓 git 以 NUL 分隔輸出原始位元組。因此 newline 模式看到
任何以 `"` 開頭的行時直接**拒絕評分**並退出——git 只有在編碼過時才會產生那個開頭，
而真的以引號開頭的檔名同樣會被 git 引起來，所以這個判準對兩邊都封閉。

⚠️ **`-z` 只解掉 C-quoting，不解掉「路徑位元組不是 UTF-8」**（盲審實測：Big5 位元組
在 `sys.stdin.encoding=utf-8` 下被讀成 `??`，於是又變成一個不存在的目錄、da-guard
exit 2、紅燈怪罪 da-guard——正是上面說已經消除的那個結果）。因此 stdin 一律讀
**bytes** 再以 `surrogateescape` 解碼、輸出時同樣以 surrogateescape 編回：非 UTF-8
的路徑逐位元組原封不動穿過這支腳本。⛔ 這是「不要在解碼那一層做判斷」的同一條規則，
只是這次的解碼是 Python 自己做的。
"""
from __future__ import annotations

import os
import sys
from pathlib import PurePosixPath

# #3579: the run() exit (_lib_exitcodes, with _lib_compat) lives in scripts/tools/.
# Appended, so a copy beside this file (one directory holding all three) wins.
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "tools"))

CONF_D = "conf.d"
EXIT_QUOTED_INPUT = 2
# ⛔ Its own code, not a second use of the one above. The two refusals need
# opposite actions from whoever reads the log — "fix the producer, pass `-z`"
# versus "rename the file" — and a constant named EXIT_QUOTED_INPUT returned
# for a path containing a TAB asserts a cause that was not established.
# ⚠️ Stated plainly: the caller does NOT branch on this. Under the step's
# `set -euo pipefail` any non-zero kills it the same way, so this buys a
# truthful log line and a test that can tell the two refusals apart — not a
# behaviour difference. Claiming more would be the kind of protection this
# file's own history is made of.
# It is the contract's 1 (scripts/tools/_lib_exitcodes.py): renaming the file
# is a change to the repo's content, which the user makes — and it stays apart
# from the 2 above, the producer's fault (#3577, #3611).
EXIT_UNREPRESENTABLE_PATH = 1
# `--deleted` given without its file: the caller's mistake, the contract's 2.
# An unreadable `--deleted` file is the same 2, via _lib_exitcodes' crash exit.
EXIT_USAGE = 2

# The output contract, named once so the refusal below is derived from the
# same two constants the emitter interpolates. Enumerating the delimiters a
# second time is how a checker drifts away from the format it guards.
FIELD_SEP = "\t"
ROW_SEP = "\n"

# ⛔ Derived from the ASCII standard, not from the shapes that were reported.
# Two independent reasons a field cannot carry one of these, and the set is
# the union so neither reason has to be re-listed:
#   * FIELD_SEP / ROW_SEP break the format outright (measured: a TAB inside a
#     path shifted the consumer's fields and made a green job name a file that
#     does not exist);
#   * every other C0 control breaks the MESSAGE. A CR is the one that already
#     reaches here — the consumer's `read` keeps it, da-guard is handed a
#     directory that does not exist, and the resulting `::error::` line is
#     redrawn by the terminal so the operator reads a path that is not the one
#     that failed. Red for an invisible reason is not better than green.
# ⚠️ The cost is real and deliberate: a Linux filename may contain any of
# these bytes, and such a file now hard-fails instead of being validated. The
# message says rename, because the alternative is a report that lies.
# ⚠️ Surrogates from `surrogateescape` (\udc80-\udcff) are NOT in this set —
# non-UTF-8 path bytes must still pass through verbatim.
# ⛔ NEL / LS / PS are deliberately NOT here, even though `splitlines()` cuts
# on them. They are representable in TSV — the consumer's `read` breaks on
# newline only — so the correct handling is to pass them through verbatim, and
# `test_a_non_c0_line_separator_survives_whole` is the half of the rule that
# pins that. ⚠️ A review round proposed widening this set to cover them, on the
# strength of a sentence further down that wrongly claimed they were already
# refused. Three existing tests refused the widening. Whoever reads that
# proposal again: the defect was the sentence, not the set.
UNREPRESENTABLE = frozenset(chr(c) for c in range(0x20)) | {"\x7f",
                                                            FIELD_SEP, ROW_SEP}

# Trees the guard deliberately does not validate, by repo-relative prefix →
# the reason the caller prints. ⛔ The one place this is defined: the workflow
# reports `excluded` rows by kind and never matches a path itself.
# tests/golden/fixtures/ encodes malformed shapes ON PURPOSE (a root
# `defaults:` holding a mapping or a string, which the exporter drops whole);
# the same reason .pre-commit-config.yaml keeps these trees out of the schema
# check. What IS pinned is each tree's `da-guard served-values` exit code
# (tests/golden/not_served.json +
# components/threshold-exporter/app/cmd/da-guard/golden_served_test.go); the main
# check's result on them is deliberately not judged anywhere.
EXCLUDED_PREFIXES = {
    "tests/golden/fixtures/": "test fixture (main check not judged; served-values exit pinned by golden_served_test.go)",
}


def excluded_reason(path: str) -> str | None:
    """The reason `path` is not validated (EXCLUDED_PREFIXES), or None.

    Matched on the verbatim string, like everything else here: a path that
    only looks like the prefix after rewriting is not excluded."""
    for prefix, reason in EXCLUDED_PREFIXES.items():
        if path.startswith(prefix):
            return reason
    return None


def partition(paths: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """(kept, excluded): `excluded` is (path, reason), sorted; `kept` keeps the
    input order. ⛔ An excluded path is still a row the caller reports — it is
    taken out of `resolve()` only so it neither becomes a target nor widens
    another tree's scope."""
    kept: list[str] = []
    excluded: list[tuple[str, str]] = []
    for path in paths:
        reason = excluded_reason(path) if path else None
        if reason is None:
            kept.append(path)
        else:
            excluded.append((path, reason))
    return kept, sorted(excluded)


def conf_d_root(path: str) -> str | None:
    """回傳 `path` 所屬的 conf.d 根（POSIX 相對路徑），找不到則 None。

    以最近的祖先為準：巢狀 conf.d（`a/conf.d/b/conf.d/x.yaml`）取內層那個，
    因為內層才是那份設定實際被解析時的根。

    ⛔ 路徑逐字使用。這裡曾經做分隔符正規化（把反斜線換成 `/`），與 `resolve()`
    開頭那個 `strip()` 同一個類別（判斷跑在被改寫過的值上），結局也一樣：反斜線
    在 POSIX 檔名裡合法，一個真的叫 `we\\ird` 的目錄被改寫成 `we/ird` ⇒ scope 指向
    一個不存在的目錄 ⇒ da-guard exit 2 ⇒ 紅燈，而訊息指著一個從沒出現在 PR diff
    裡的路徑（實測）。輸入永遠來自 Linux 的 `git diff -z`，分隔符只可能是 `/`。
    """
    p = PurePosixPath(path)
    for parent in p.parents:
        if parent.name == CONF_D:
            return str(parent)
    return None


def resolve(paths: list[str], extra: dict[str, set[str]] | None = None,
            ) -> tuple[list[tuple[str, str]], list[str]]:
    """(targets, unmanaged)；targets 為 (config_dir, scope)，依 config_dir 排序。

    `extra`：root → 額外要併進該根的目錄（repo 相對），來自 `place()`——
    內層被整棵刪掉、外層還在的情形（#3694）。與變更檔的目錄走同一條收窄／放寬
    規則：同一個根底下合計只有一個目錄才收窄，否則整棵重驗。"""
    by_root: dict[str, set[str]] = {
        root: set(dirs) for root, dirs in (extra or {}).items()}
    unmanaged: list[str] = []
    for raw in paths:
        # ⛔ The path is used AS GIVEN. This used to be `raw.strip()`, which is
        # the TAB defect wearing a quieter hat: ` conf.d/db/_defaults.yaml`
        # (one leading space — a real, distinct directory on Linux) was
        # rewritten into `conf.d/db/_defaults.yaml`, which resolves to ANOTHER
        # tree that exists, and da-guard then validated that other tree and the
        # sticky comment reported success. Same ending as #1219: green, about a
        # tree the PR never touched.
        # ⚠️ Only the genuinely empty string is skipped, and only because the
        # separators are terminators — `git diff -z` ends every record with a
        # NUL, so the final split piece is always "". A whitespace-only entry
        # is NOT skipped: it has no conf.d ancestor, so it lands in `unmanaged`
        # and the caller says NOT CHECKED out loud. Dropping it here would be
        # the silent skip this module exists to remove.
        if not raw:
            continue
        path = raw
        root = conf_d_root(path)
        if root is None:
            unmanaged.append(path)
            continue
        # 同上：逐字，不做分隔符正規化。
        parent = str(PurePosixPath(path).parent)
        by_root.setdefault(root, set()).add(parent)

    targets: list[tuple[str, str]] = []
    for root in sorted(by_root):
        dirs = by_root[root]
        # 單一目錄才收窄；多個目錄代表串接編輯，整棵重驗才驗得出跨層的
        # redundant-override（沿用原 workflow 的語意）。
        scope = next(iter(dirs)) if len(dirs) == 1 else root
        # da-guard resolves a relative --scope against --config-dir (#2588),
        # so the scope is emitted relative to the root ("." = the root).
        # relative_to on the verbatim strings: no normalisation (see above).
        targets.append((root, str(PurePosixPath(scope).relative_to(root))))
    return targets, sorted(unmanaged)


def removed_roots(paths: list[str], deleted: set[str],
                  exists=os.path.lexists) -> set[str]:
    """The conf.d roots this change removed whole (#3644): every changed path
    under the root is in `deleted`, AND the root is gone from the checkout.

    ⛔ Both, never the second alone — a root that does not exist is also what a
    path rewritten on its way here looks like, and that case must stay a red
    `target` (see the module docstring). `deleted` comes from git itself, so a
    rewritten path does not appear in it verbatim and fails condition 1.
    Paths are compared verbatim, like everything else in this file.
    """
    by_root: dict[str, list[str]] = {}
    for path in paths:
        if not path:
            continue
        root = conf_d_root(path)
        if root is not None:
            by_root.setdefault(root, []).append(path)
    return {root for root, members in by_root.items()
            if all(p in deleted for p in members) and not exists(root)}


def place(paths: list[str], deleted: set[str], exists=os.path.lexists,
          ) -> tuple[list[str], dict[str, set[str]], set[tuple[str, str]]]:
    """Split changed paths by what their conf.d ancestors look like AT HEAD.

    Returns (plain, extra, nested):
    * `nested` — (inner, outer) for a path with two or more conf.d ancestors
      that still exist: the layout this module refuses (module docstring).
    * `extra` — outer root → scope dirs, for a DELETED path whose nearest
      conf.d is gone while an outer one survives: the PR removed the inner
      tree, the outer tree changed, and nothing is nested any more (#3694).
      The scope is the deepest directory on the way up that still exists,
      never one that does not (that would be da-guard exit 2 about nothing).
    * `plain` — everything else, for `resolve()` / `removed_roots()` exactly
      as before. ⛔ That includes a path whose nearest conf.d is missing but
      which is NOT in `deleted`: a rewritten path must stay a red target on
      its nearest root, never be re-homed onto a tree that happens to exist.
    """
    plain: list[str] = []
    extra: dict[str, set[str]] = {}
    nested: set[tuple[str, str]] = set()
    for path in paths:
        if not path:
            continue
        parents = PurePosixPath(path).parents
        ancestors = [str(p) for p in parents if p.name == CONF_D]
        alive = [a for a in ancestors if exists(a)]
        pair = _nesting(alive)
        if pair is not None:
            nested.add(pair)
            continue
        if ancestors and alive and alive[0] != ancestors[0] and path in deleted:
            outer = alive[0]
            scope = next(str(p) for p in parents
                         if str(p) == outer or exists(str(p)))
            extra.setdefault(outer, set()).add(scope)
            continue
        plain.append(path)
    return plain, extra, nested


def walker_reaches(outer: str, inner: str, islink=os.path.islink) -> bool:
    """Whether the exporter's walker, started at `outer`, enters `inner`.

    It prunes `.`-prefixed directories (confdname.IsHidden) and does not
    follow directory symlinks (tree_scan.go, #1972), so a conf.d behind either
    is not part of the outer tree's effective config — not nesting."""
    current = PurePosixPath(outer)
    for part in PurePosixPath(inner).relative_to(outer).parts:
        current = current / part
        if part.startswith(".") or islink(str(current)):
            return False
    return True


def _nesting(alive: list[str], reaches=walker_reaches) -> tuple[str, str] | None:
    """(innermost, outermost) existing conf.d pair the walker joins, or None.
    `alive` is nearest-first, as `place()` builds it."""
    for i, inner in enumerate(alive):
        for outer in reversed(alive[i + 1:]):
            if reaches(outer, inner):
                return inner, outer
    return None


def nested_under(roots, isdir=os.path.isdir, walk=os.walk,
                 islink=os.path.islink) -> set[tuple[str, str]]:
    """(inner, root) for every non-hidden `conf.d` directory found below a
    target root in the checkout — a PR that edits only the outer tree still
    ships a nested layout, and the outer tree's effective config contains the
    inner one. Pruned the way the exporter's walker prunes: hidden directories
    (confdname.IsHidden: a leading dot) and directory symlinks (not followed,
    #1972). ⛔ These names come from the disk, not from the PR's path list, so
    they have NOT passed the UNREPRESENTABLE check — main() runs it on them."""
    found: set[tuple[str, str]] = set()
    for root in roots:
        if not isdir(root):
            continue
        for current, dirs, _files in walk(root):
            # os.walk does not descend into a directory symlink but still
            # LISTS it; the walker does not follow one either (#1972).
            dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                             and not islink(os.path.join(current, d)))
            for d in dirs:
                if d == CONF_D:
                    found.add((str(PurePosixPath(current) / d), root))
    return found


def read_deleted(path: str) -> set[str]:
    """The `--deleted` file: NUL-separated (`git diff -z`), read as bytes and
    decoded with surrogateescape for the same reason stdin is (see main)."""
    with open(path, "rb") as fh:
        raw = fh.read().decode("utf-8", errors="surrogateescape")
    return {p for p in raw.split("\0") if p}


def looks_c_quoted(path: str) -> bool:
    """git 的 C-quoting 一定以 `"` 開頭；真的以引號開頭的檔名也會被引起來。

    所以這個判準對兩個方向都封閉：不需要列舉跳脫序列的形狀，也不會漏掉
    「剛好只含一個非 ASCII 字元」這種最短的編碼。
    """
    return path.startswith('"')


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    deleted_file: str | None = None
    if "--deleted" in args:
        i = args.index("--deleted")
        if i + 1 >= len(args):
            sys.stderr.write("guard_defaults_scopes: --deleted needs a file "
                             "(NUL-separated output of `git diff -z --name-only "
                             "--diff-filter=D ...`).\n")
            return EXIT_USAGE
        deleted_file = args[i + 1]
        del args[i:i + 2]
    null_sep = False
    for flag in ("--null", "-0"):
        if flag in args:
            null_sep = True
            args = [a for a in args if a != flag]

    if args == ["-"] or not args:
        # ⛔ bytes → surrogateescape, never `sys.stdin.read()`. A path whose
        # bytes are not UTF-8 (a Big5-encoded directory name, say) decodes to
        # `??` under the default reader, which lands on a directory that does
        # not exist — the same red-blaming-da-guard outcome `-z` was supposed to
        # remove. surrogateescape round-trips those bytes verbatim.
        raw = sys.stdin.buffer.read().decode("utf-8", errors="surrogateescape")
        if null_sep:
            paths = raw.split("\0")
        else:
            # ⛔ `split("\n")`, never `splitlines()`. `splitlines()` breaks on
            # ELEVEN separators, so a path containing \r, \v, \f, \x1c-\x1e,
            # \x85,   or   was silently cut in two and the tail
            # emitted as its own `unmanaged` row — measured, all eleven produce
            # `unmanaged<TAB>ird/_defaults.yaml`, rc 0, and the caller then
            # reports a file that does not exist as NOT CHECKED while staying
            # green. That is byte-for-byte the outcome the refusal below exists
            # to stop, walking straight past it because the refusal inspects
            # the pieces AFTER the split. It also made the `"\n" in p` half of
            # that refusal unreachable in this mode: the newline was consumed
            # before anything could see it.
            # ⛔ 切開之後**不再動任何一個字元**。這裡曾經是
            # `p[:-1] if p.endswith("\r") else p`，也就是 `raw.strip()` 縮到
            # 一個字元，而且跑在下面那道拒絕之前——於是同一串位元組在兩個模式得到
            # 相反答案（實測：`conf.d/db` 尾隨一個 CR，在 `--null` 下 rc=3 被拒，
            # 在 newline 模式下 rc=0 且悄悄剝成 `conf.d`）。CR 已經在
            # UNREPRESENTABLE 裡，剝掉它等於把一個該被拒絕的輸入洗成可接受的，
            # 正是同一支腳本的拒絕訊息寫著「Do NOT strip the character」的那件事。
            paths = raw.split("\n")
    else:
        paths = args

    # ⛔ NEWLINE MODE ONLY, and the reason is a measurement rather than an
    # argument. A previous version ran this in both modes on the premise that
    # "a genuine NUL-separated path never starts with a double quote — git only
    # produces that when it encoded". That is FALSE: `-z` disables quoting
    # entirely, so a directory legitimately named `"quoted-dir` comes through
    # raw. Measured with `git mktree -z`:
    #   git diff --name-only      ->  "\"quoted-dir/conf.d/_defaults.yaml"
    #   git diff -z --name-only   ->  "quoted-dir/conf.d/_defaults.yaml\0
    # so in NUL mode the refusal fired on a legal name, killed the step under
    # `set -euo pipefail`, and told the operator to "fix the producer — pipe
    # `git diff -z` into this script with `--null`", which is exactly what the
    # producer already does. A dead-end message on a false red.
    # In newline mode the predicate stays closed in both directions: git wraps
    # a name that starts with a quote in ANOTHER quote, so every `"`-leading
    # line there is encoded output, and refusing is the only safe answer.
    # ⚠️ The mis-fed combination this once tried to catch (`--null` kept, `-z`
    # dropped) is prevented where it can actually be checked:
    # `test_the_scope_helper_is_told_that_its_input_is_nul_separated` pins the
    # two flags to the same step.
    # ⚠️ Tested on the RAW value, for the same reason `resolve()` no longer
    # strips: git's newline-mode encoding always puts the quote at column 0,
    # and a name that genuinely begins with whitespace-then-quote contains a
    # quote, so git wraps that too. Stripping first could only ever move the
    # predicate off the bytes it is about.
    quoted = [] if null_sep else [p for p in paths if looks_c_quoted(p)]
    if quoted:
        sys.stderr.write(
            "guard_defaults_scopes: refusing to score C-quoted input — "
            f"{len(quoted)} path(s) start with a double quote, e.g. {quoted[0]!r}.\n"
            "git encodes non-ASCII paths that way unless told otherwise, and "
            "decoding them here cannot be done safely (see this file's "
            "docstring).\n"
            "⛔ Do NOT drop these paths and do NOT strip the quotes: fix the "
            "producer instead — `git diff -z --name-only ...` piped into this "
            "script with `--null`.\n")
        return EXIT_QUOTED_INPUT

    # ⛔ A path that contains the output format's own delimiters cannot be
    # emitted at all. The consumer reads this with `while IFS=$'\t' read -r
    # kind a b`, so a TAB inside a path silently shifts the fields: measured,
    # `we<TAB>ird/_defaults.yaml` produced `unmanaged\twe\tird/…`, the caller's
    # row-shape check passed (both fields non-empty), and the sticky comment
    # reported `### `we` — NOT CHECKED` — a green job naming a file that does
    # not exist.
    # ⚠️ Closure has to name its LAYER. This is closed over the OUTPUT format
    # — the delimiters come from FIELD_SEP / ROW_SEP, the same two names the
    # emitter interpolates, so the checker cannot drift away from the format.
    # An earlier version of this comment said "closed, not a spelling list"
    # full stop, while the INPUT splitter above was `splitlines()`; the check
    # then ran on pieces that had already been split and saw nothing. Closure
    # over the output means nothing if something upstream reshapes the input
    # first — the splitter is `split("\n")` for exactly that reason.
    # ⚠️ Measured, because the earlier phrasing of THIS sentence gave the
    # wrong reason: `splitlines()` cuts on ten characters here and
    # `split("\n")` on one, but ROW_SEP is not what separates them. A literal
    # newline inside a path is unreachable under either splitter — it is the
    # OTHER nine (\v \f \r \x1c \x1d \x1e \x85 \u2028 \u2029) that survive
    # `split("\n")` intact and are therefore still there to be refused.
    # \u26a0\ufe0f \u2026and "refused" is wrong for three of those nine. Measured end to end,
    # one process call per character: \v \f \r \x1c \x1d \x1e give `rc=3`;
    # NEL, LS and PS give `rc=0` and are emitted VERBATIM inside the row.
    # \u26d4 That is correct, not a gap \u2014 they are representable in TSV, so passing
    # them through is the specified behaviour and
    # `test_a_non_c0_line_separator_survives_whole` pins it. What all nine
    # share is only that they survive `split("\n")`; six are then refused for
    # being unrepresentable and three are carried. A review round read this
    # sentence, concluded the set had a hole, and proposed widening
    # UNREPRESENTABLE \u2014 three existing tests refused the widening. The
    # sentence was the defect; the set was already right.
    # ⚠️ And the set is wider than the two delimiters, deliberately: see
    # UNREPRESENTABLE. A CR is representable in TSV and still ruinous, because
    # it makes the failure message describe a path the operator never has.
    unrepresentable = [p for p in paths if UNREPRESENTABLE.intersection(p)]
    if unrepresentable:
        first = unrepresentable[0]
        offenders = sorted(UNREPRESENTABLE.intersection(first))
        sys.stderr.write(
            "guard_defaults_scopes: refusing to emit a row for "
            f"{len(unrepresentable)} path(s) containing a control character, "
            f"e.g. {first!r} (contains {', '.join(repr(c) for c in offenders)}"
            ").\n"
            "The output is TSV read line by line, so a TAB or newline cannot "
            "be represented at all — emitting it would shift the consumer's "
            "fields and make it report a different (non-existent) file as NOT "
            "CHECKED. Any other control character survives the format but not "
            "the log: the annotation naming the failure gets redrawn and shows "
            "a path that is not the one that failed.\n"
            "⛔ Do NOT strip the character and do NOT skip the path: rename the "
            "file, or change this contract on both ends together.\n")
        return EXIT_UNREPRESENTABLE_PATH

    kept, excluded = partition(paths)
    deleted = read_deleted(deleted_file) if deleted_file is not None else set()
    # #3694 follow-up: nested layouts are refused, and an inner tree removed
    # under a surviving outer one makes the outer one a target. `deleted` is
    # empty without `--deleted`, so neither re-homing nor `removed` happens and
    # the failure direction stays "validated again and red".
    plain, extra, nested = place(kept, deleted)
    # ⛔ No `--deleted` ⇒ no deletion facts ⇒ nothing is `removed`: the tree
    # stays a target and da-guard's exit 2 keeps the job red. Never inferred
    # from the directory being absent alone (see removed_roots).
    # Over ALL kept paths, not just `plain`: an inner tree re-homed onto its
    # surviving outer tree is still named as removed. A nested path cannot land
    # here — its nearest root exists, and `removed_roots` requires it gone.
    removed = (removed_roots(kept, deleted)
               if deleted_file is not None else set())
    targets, unmanaged = resolve(plain, extra)
    targets = [(root, scope) for root, scope in targets if root not in removed]
    nested |= nested_under(root for root, _scope in targets)
    # ⛔ Same refusal as the input paths, for names that came off the disk: a
    # TAB or newline in a directory that holds no `_` file is not in the diff,
    # and emitting it would shift the consumer's fields (blind review).
    bad = sorted(inner for inner, _outer in nested
                 if UNREPRESENTABLE.intersection(inner))
    if bad:
        sys.stderr.write(
            "guard_defaults_scopes: refusing to emit a `nested` row for a "
            f"directory whose name contains a control character: {bad[0]!r}.\n"
            "⛔ Rename the directory; do NOT strip the character.\n")
        return EXIT_UNREPRESENTABLE_PATH
    lines = [f"target{FIELD_SEP}{config_dir}{FIELD_SEP}{scope}{ROW_SEP}"
             for config_dir, scope in targets]
    lines += [f"removed{FIELD_SEP}{root}{ROW_SEP}" for root in sorted(removed)]
    lines += [f"nested{FIELD_SEP}{inner}{FIELD_SEP}{outer}{ROW_SEP}"
              for inner, outer in sorted(nested)]
    lines += [f"unmanaged{FIELD_SEP}{path}{ROW_SEP}" for path in unmanaged]
    lines += [f"excluded{FIELD_SEP}{path}{FIELD_SEP}{reason}{ROW_SEP}"
              for path, reason in excluded]
    # ⛔ Written as bytes with the same surrogateescape handler that read them,
    # and with `\n` pinned. Going back through `sys.stdout` would re-encode
    # strictly (UnicodeEncodeError on a surrogate) and, on Windows, would turn
    # every `\n` into `\r\n` — the consuming `while IFS=$'\t' read -r` then
    # carries a CR into the scope it hands da-guard.
    payload = "".join(lines).encode("utf-8", errors="surrogateescape")
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
