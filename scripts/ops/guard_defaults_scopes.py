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
  git diff -z --name-only ... | python3 scripts/ops/guard_defaults_scopes.py --null -

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

import sys
from pathlib import PurePosixPath

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
EXIT_UNREPRESENTABLE_PATH = 3

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


def resolve(paths: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """(targets, unmanaged)；targets 為 (config_dir, scope)，依 config_dir 排序。"""
    by_root: dict[str, set[str]] = {}
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
        targets.append((root, scope))
    return targets, sorted(unmanaged)


def looks_c_quoted(path: str) -> bool:
    """git 的 C-quoting 一定以 `"` 開頭；真的以引號開頭的檔名也會被引起來。

    所以這個判準對兩個方向都封閉：不需要列舉跳脫序列的形狀，也不會漏掉
    「剛好只含一個非 ASCII 字元」這種最短的編碼。
    """
    return path.startswith('"')


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
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

    targets, unmanaged = resolve(paths)
    lines = [f"target{FIELD_SEP}{config_dir}{FIELD_SEP}{scope}{ROW_SEP}"
             for config_dir, scope in targets]
    lines += [f"unmanaged{FIELD_SEP}{path}{ROW_SEP}" for path in unmanaged]
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
    raise SystemExit(main())
