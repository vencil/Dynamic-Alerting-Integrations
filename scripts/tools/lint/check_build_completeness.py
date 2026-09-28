#!/usr/bin/env python3
"""check_build_completeness.py — build.sh ↔ COMMAND_MAP 雙向同步檢查。

確保 Docker image 包含 COMMAND_MAP 引用的所有工具腳本，
同時確保 build.sh 中的每個工具都有對應的 COMMAND_MAP 條目。

v2.4.0 新增：解決 v2.3.0 release 過程中 opa-evaluate 加入 COMMAND_MAP
但遺漏 build.sh TOOL_FILES 導致 Docker image 中 da-tools crash 的問題。

另含兩條 image-completeness 延伸規則（da-tools image 打包 bugfix 防再犯，
起因：threshold_recommend.py 頂層 `import _observed_map_lib` 但 TOOL_FILES
漏列該 lib 與其資料檔 → image 內 threshold-recommend / threshold-govern
雙雙 ImportError）：

  1. transitive underscore-import 掃描 — TOOL_FILES 中每個 shipped .py 以
     ast 解析其 `import _xxx` / `from _xxx import`（含函式層級 import）；
     若 `_xxx.py` 是 scripts/tools 樹內的實體 sibling 檔案，就必須也在
     TOOL_FILES（否則 image 內 ImportError）。非 repo 檔案的底線模組
     （stdlib / dunder）自動略過，describe_tenant 這類「非底線」註解式
     維護的相依不在守備範圍（維持既有人工註解慣例）。
  2. REQUIRED_DATA_FILES 對照 — import 掃不到資料檔；已驗證「同目錄尋址
     + 缺檔 fail-quiet」的 (module → data file) 顯式列管：模組在
     TOOL_FILES 時其資料檔也必須在（否則工具靜默空轉，比 crash 更難察覺）。

用法:
    python3 scripts/tools/lint/check_build_completeness.py [--ci] [--json]
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

from _lint_helpers import (
    parse_command_map,
    parse_build_sh_tools,
    parse_build_sh_tool_paths,
    parse_build_sh_repo_data_files,
    BUILD_EXEMPT,
    ENTRYPOINT_PATH,
    BUILD_SH_PATH,
    REPO_ROOT,
)
import os

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

# Shipped tools live under scripts/tools/ (build.sh TOOL_FILES paths are
# relative to this dir; cp flattens them into the image's /app).
TOOLS_SRC = REPO_ROOT / "scripts" / "tools"

# Subdirectories under scripts/tools/ that hold shipped sibling modules a tool
# may import ("" = the tools root itself). The underscore-import scan resolves
# each imported `_xxx` against these; extend this list when a new tools
# subdirectory appears so the scan's scope stays complete.
_SIBLING_MODULE_DIRS = ("", "ops", "dx", "lint")

# 資料檔無法用 import 掃 → 顯式列管（module → data files 皆為 basename，
# 需同時出現在 TOOL_FILES）。只收「已驗證同目錄尋址」的組合：
#   - _observed_map_lib.py: DEFAULT_MAP_PATH = <lib 同目錄>/metric_observed_map.yaml，
#     且 load_observed_map() 對缺檔回 {} → 全 key 靜默 skip（fail-quiet，#719）。
#   - analyze_rule_pack_gaps.py: DEFAULT_METRIC_DICT = <script 同目錄>/metric-dictionary.yaml
#     （flat image layout 下即 /app/metric-dictionary.yaml）。
# migrate_rule / generate_tenant_mapping_rules 也讀 metric-dictionary.yaml，
# 但走「自動偵測」非單一同目錄預設；上面的 analyze_rule_pack_gaps 條目已足以
# 把該資料檔錨在 TOOL_FILES，故不重複列。
#   - _grar_validate.py: _find_platform_rules_configmap() 先找 <module 同目錄>/
#     configmap-rules-platform.yaml（映像 flat layout），找不到才往上找 repo 的
#     k8s/03-monitoring/。缺檔時 platform_alert_identities() 退回 6 筆常數，
#     而完整 pack 是 41 筆（實測）→ 少掉的 35 個 alertname 不會被
#     find_tenant_silenceable_platform_inhibits 探測，是 fail-OPEN 方向（#1494）。
#     該檔不在 scripts/tools/ 底下，由 build.sh 的 REPO_DATA_FILES 搬運。
#   - _threshold_alerts.py: find_rule_pack_paths() 先找 <module 同目錄>/
#     rule-pack-*.yaml（映像 flat layout），找不到才找 repo 的 rule-packs/。
#     缺檔時 config-diff 的 Affected Alerts 欄寫 unknown、stderr 一行 WARN
#     （不猜）；但少出貨「某一個」pack 時不會 unknown，而是該 pack 的 key 被
#     報成「沒有告警讀它」——那是 fail-OPEN，所以逐檔列管。清單＝repo 裡引用
#     alert_threshold: 的 pack，由 tests/ops/test_threshold_alerts.py 對帳。
#   - validate_config.py: _find_schema() 先找 <module 同目錄>/<name>.schema.json
#     （映像 flat layout），找不到才找 repo 的 docs/schemas/。缺檔時
#     yaml_quoting 列 FAIL + caller_error（exit 2），不靜默略過（#2164）；
#     platform-defaults 以 $ref 引用 tenant-config，所以兩份必須同船；
#     routing-profiles（#2245）同理，也以 $ref 引用 tenant-config。
REQUIRED_DATA_FILES: dict = {
    "validate_config.py": ("tenant-config.schema.json",
                           "platform-defaults.schema.json",
                           "routing-profiles.schema.json"),
    "_observed_map_lib.py": ("metric_observed_map.yaml",),
    "analyze_rule_pack_gaps.py": ("metric-dictionary.yaml",),
    "_grar_validate.py": ("configmap-rules-platform.yaml",),
    "_lib_validation.py": ("tenant-config.schema.json",),
    "_threshold_alerts.py": (
        "rule-pack-clickhouse.yaml",
        "rule-pack-db2.yaml",
        "rule-pack-elasticsearch.yaml",
        "rule-pack-jvm.yaml",
        "rule-pack-kafka.yaml",
        "rule-pack-kubernetes.yaml",
        "rule-pack-mariadb.yaml",
        "rule-pack-mongodb.yaml",
        "rule-pack-nginx.yaml",
        "rule-pack-oracle.yaml",
        "rule-pack-postgresql.yaml",
        "rule-pack-rabbitmq.yaml",
        "rule-pack-redis.yaml",
    ),
}

# 映像把每支工具攤平到 WORKDIR（Dockerfile `WORKDIR /opt/da-tools` + build.sh
# 的 `cp <src> tools/`），所以 `/opt/da-tools/x.py` 只有 3 個祖先
# （/opt/da-tools、/opt、/）→ 從 `__file__` 合法的上溯上限是 **2 層**
# （`.parent` ×2 ≡ `parents[1]` → `/opt`；第 3 層就是 `/`）。⛔ 這是層數，不是
# `parents[]` 的 index——`parents[N]` 是 N+1 層（#1503 盲點 1）。repo 佈局下
# 同一支檔案有 8~9 個，因此任何「數上去幾層」的寫法在兩種佈局下必然分岔，
# 而 repo 佈局會把它藏住。
MAX_IMAGE_ANCESTOR_INDEX = 2


_APPEND_ARRAYS = ("TOOL_FILES", "REPO_DATA_FILES")


def check_append_form_arrays(build_sh: Path = None) -> list:
    """`NAME+=( … )` 在 build.sh 裡是禁止的，因為 reader 讀不到它。

    ⛔ 這條是把一個**揭露**升級成守衛，因為那個揭露的減災說法是假的。原本寫的是
    「`+=` 讀不到，但方向是少撈，雙向檢查會大聲」——實測只有兩種情形會大聲：
    該檔在 `COMMAND_MAP` 裡，或某支已出貨模組 import 它。函式庫、資料檔、以及
    `BUILD_EXEMPT` 的 daemon 三類都不在其中，用 `+=` 加進來會**全綠**，而且
    `check_layout_depth_assumptions`（#1494 自己那條規則）根本不會掃到它。

    reader 之所以讀不到，是因為文字掃描分不出「指派」與「提及」（試圖分辨的那
    一版讓 `echo "… TOOL_FILES=( … )"` 取代了整個陣列）。所以正解不是讓 reader
    更聰明，而是讓**這種寫法不存在**——一行 tripwire，訊息直接給替代寫法。
    """
    src = (build_sh or BUILD_SH_PATH).read_text(encoding="utf-8-sig")
    errors = []
    for name in _APPEND_ARRAYS:
        pattern = re.compile(
            r"^(?:(?:local|declare|export|readonly|typeset)(?:\s+-\w+)*\s+)?"
            + re.escape(name) + r"\+=\(", re.M)
        for m in pattern.finditer(src):
            line = src[:m.start()].count("\n") + 1
            errors.append((
                "error",
                f"build.sh:{line} 用了 `{name}+=(`。出貨清單的 reader 讀不到"
                f"追加形式（文字掃描分不出指派與提及），所以這樣加進來的檔案會"
                f"**靜默地**不被層數守衛與配對檢查涵蓋。請併進同名陣列的單一"
                f"`{name}=(` 區塊。"
            ))
    return errors


def check_bidirectional(command_map: dict, build_tools: set) -> list:
    """雙向比對 COMMAND_MAP 與 build.sh TOOL_FILES。

    Returns:
        list of (severity, message) tuples
    """
    errors = []

    # Direction 1: COMMAND_MAP → build.sh
    # 每個 COMMAND_MAP 指向的 .py 都必須在 build.sh 中
    cm_scripts = set(command_map.values())
    missing_in_build = cm_scripts - build_tools
    for script in sorted(missing_in_build):
        cmd = [k for k, v in command_map.items() if v == script][0]
        errors.append((
            "error",
            f"COMMAND_MAP 有 '{cmd}' → '{script}' 但 build.sh TOOL_FILES 缺少此檔案。"
            f" Docker image 中 `da-tools {cmd}` 會 crash。"
        ))

    # Direction 2: build.sh → COMMAND_MAP
    # 每個非豁免的 .py 應有 COMMAND_MAP 條目
    build_py_only = {f for f in build_tools if f.endswith(".py")} - BUILD_EXEMPT
    mapped_scripts = set(command_map.values())
    orphan_in_build = build_py_only - mapped_scripts
    for script in sorted(orphan_in_build):
        errors.append((
            "warning",
            f"build.sh TOOL_FILES 有 '{script}' 但 COMMAND_MAP 中沒有對應命令。"
            f" 此工具被打包但無法透過 `da-tools <cmd>` 呼叫。"
        ))

    return errors


def _underscore_imports_of(source: str) -> set:
    """Top-level names of sibling-style underscore imports in ``source``.

    Walks ALL Import/ImportFrom nodes (function-level imports crash at call
    time in the image just the same). Dunder modules (``__future__`` etc.)
    are excluded; relative imports (level>0) don't occur in the flat layout.
    """
    names: set = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return {
        n for n in names
        if n.startswith("_") and not n.startswith("__")
    }


def check_underscore_imports(
    tool_rel_paths: set, build_tools: set, tools_src: Path = None
) -> list:
    """掃 TOOL_FILES 中每個 shipped .py 的 sibling 底線模組 import。

    import 的 ``_xxx`` 若解析得到 scripts/tools 樹內的實體 ``_xxx.py``
    （root / ops/ / dx/ / lint/），該檔就必須也在 TOOL_FILES —— 否則
    flat image layout 內 import 直接 ImportError（threshold_recommend →
    _observed_map_lib 事故的防再犯）。找不到對應 repo 檔案的底線模組視為
    stdlib / 外部套件，略過（不 false-fire）。

    Returns:
        list of (severity, message) tuples
    """
    tools_src = tools_src or TOOLS_SRC
    errors = []
    for rel in sorted(tool_rel_paths):
        if not rel.endswith(".py"):
            continue
        src_path = tools_src / rel
        if not src_path.is_file():
            # 檔案不存在交給 build.sh 自身的存在性檢查（cp 會 fail），
            # 這裡不重複報。
            continue
        try:
            # utf-8-sig for the same reason as the layout rule below: a BOM is
            # invisible to Python's import machinery but makes `ast.parse`
            # raise, so a runnable tool would be reported as unparseable.
            # ⛔ Fixed here TOO, not only where it was noticed — one誤紅 in one
            # function of this file is the same defect as in the other.
            imported = _underscore_imports_of(
                src_path.read_text(encoding="utf-8-sig"))
        # ⛔ Three exception types, not one. `read_text` raises OSError for an
        # unreadable file and UnicodeDecodeError for a non-UTF-8 one; catching
        # only SyntaxError let either escape and abort main() with a traceback
        # instead of producing an error entry — i.e. the scanner reported
        # NOTHING about that file and everything after it. The sibling scanner
        # below already caught all three, so this was the same defect the
        # comment above claims to have fixed "here too", still half-applied.
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            errors.append((
                "error",
                f"'{rel}' 無法讀取或以 ast 解析（{type(exc).__name__}: {exc}）—"
                f" 無法驗證其 import 完整性。"
            ))
            continue
        for name in sorted(imported):
            mod_file = f"{name}.py"
            candidates = tuple(
                (tools_src / sub / mod_file) if sub else (tools_src / mod_file)
                for sub in _SIBLING_MODULE_DIRS
            )
            if not any(c.is_file() for c in candidates):
                continue  # 非 repo sibling（stdlib/外部）→ 不在守備範圍
            if mod_file not in build_tools:
                errors.append((
                    "error",
                    f"'{rel}' import 了 sibling 模組 '{name}'，但 build.sh"
                    f" TOOL_FILES 缺少 '{mod_file}'。Docker image（flat layout）"
                    f"內 import 會 ImportError。"
                ))
    return errors


class _Ascent(NamedTuple):
    """How far an expression rooted at ``__file__`` sits above the file.

    ``cur`` is the net level the expression ENDS at (``__file__`` is 0, its
    directory 1). ``peak`` is the highest level any step *inside this
    expression* reaches — the number that decides saturation, because
    ``os.path.join(d, "..", "..", "..", "rule-packs")`` ends one level lower
    than it climbed, and it is the climb that hits ``/``. A name looked up in
    the assignment table contributes its ``cur`` but NOT a peak: its own
    definition line is where its climb is reported, so every later use does
    not repeat the finding.
    """

    cur: int
    peak: int
    unknown: bool
    kinds: frozenset


_NO_PEAK = -1
# Callables that hand back (a form of) their first argument's path.
_PASSTHROUGH_FUNCS = frozenset({
    "abspath", "realpath", "normpath", "fspath", "str", "expanduser",
    "Path", "PurePath", "PosixPath", "PurePosixPath", "WindowsPath",
    "PureWindowsPath",
})
# Of those, the pathlib constructors also JOIN their further arguments.
_JOINING_CTORS = frozenset({
    "Path", "PurePath", "PosixPath", "PurePosixPath", "WindowsPath",
    "PureWindowsPath",
})
_OS_PATH_OWNERS = frozenset({"path", "posixpath", "ntpath", "osp"})


def _func_name(func: ast.AST) -> "str | None":
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_os_path_func(func: ast.AST, name: str) -> bool:
    """``os.path.<name>`` / ``path.<name>`` / a bare ``<name>`` import."""
    if isinstance(func, ast.Name):
        return func.id == name
    if not (isinstance(func, ast.Attribute) and func.attr == name):
        return False
    owner = func.value
    return ((isinstance(owner, ast.Attribute) and owner.attr == "path")
            or (isinstance(owner, ast.Name) and owner.id in _OS_PATH_OWNERS))


def _segment_steps(node: ast.AST) -> "list[int]":
    """Level changes one join argument makes: ``+1`` per ``..``, ``-1`` per name.

    ⛔ A segment this reader cannot evaluate (a variable, a ``*parts``) counts
    as **0**, not as a descent. Counting it as ``-1`` would let
    ``join(d, var, "..", "..", "..")`` net out below the threshold — i.e. the
    unknown would be spent in the permissive direction.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        steps = []
        for part in node.value.replace("\\", "/").split("/"):
            if part == "..":
                steps.append(1)
            elif part not in ("", "."):
                steps.append(-1)
        return steps
    if ((isinstance(node, ast.Attribute) and node.attr == "pardir")
            or (isinstance(node, ast.Name) and node.id == "pardir")):
        return [1]
    return [0]


def _apply_segments(base: _Ascent, segments) -> _Ascent:
    cur, peak, kinds = base.cur, base.peak, base.kinds
    for seg in segments:
        for step in _segment_steps(seg):
            cur += step
            if step > 0:
                # Only a CLIMB moves the peak: descending from an inherited
                # level is not this expression reaching it.
                kinds = kinds | {"chain"}
                peak = max(peak, cur)
    return _Ascent(cur, peak, base.unknown, kinds)


def _climb(base: _Ascent, levels: int, kind: str) -> _Ascent:
    cur = base.cur + levels
    return _Ascent(cur, max(base.peak, cur), base.unknown,
                   base.kinds | {kind})


def _evaluate_ascent(node: ast.AST, names: dict) -> "_Ascent | None":
    """Where *node* sits relative to ``__file__``, or None if it is not rooted there.

    Rooted means: built from ``__file__`` itself or from a name whose
    assignment this scan already evaluated (``names``). Four spellings climb:
    ``parents[N]`` (N+1 levels), ``.parent``, ``dirname(...)``, and a literal
    ``..`` / ``os.pardir`` join segment (``os.path.join``, ``joinpath``,
    ``Path(a, b)``, the ``/`` operator).
    """
    if isinstance(node, ast.Name):
        if node.id == "__file__":
            return _Ascent(0, 0, False, frozenset())
        entry = names.get(node.id)
        if entry is None:
            return None
        return _Ascent(entry.cur, _NO_PEAK, False, entry.kinds)
    if isinstance(node, ast.Attribute):
        if node.attr == "parent":
            base = _evaluate_ascent(node.value, names)
            return None if base is None else _climb(base, 1, "chain")
        return None
    if isinstance(node, ast.Subscript):
        target = node.value
        if not (isinstance(target, ast.Attribute) and target.attr == "parents"):
            return None
        base = _evaluate_ascent(target.value, names)
        if base is None:
            return None
        idx = node.slice
        if isinstance(idx, ast.Constant) and isinstance(idx.value, int):
            # ⛔ parents[N] is N+1 levels: parents[0] is already the directory.
            return _climb(base, idx.value + 1, "index")
        return _Ascent(base.cur, base.peak, True, base.kinds | {"index"})
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        base = _evaluate_ascent(node.left, names)
        return None if base is None else _apply_segments(base, [node.right])
    if not isinstance(node, ast.Call):
        return None

    func = node.func
    if _is_os_path_func(func, "dirname"):
        base = _evaluate_ascent(node.args[0], names) if node.args else None
        return None if base is None else _climb(base, 1, "chain")
    if _is_os_path_func(func, "join") and node.args:
        base = _evaluate_ascent(node.args[0], names)
        return None if base is None else _apply_segments(base, node.args[1:])
    if isinstance(func, ast.Attribute) and func.attr == "joinpath":
        base = _evaluate_ascent(func.value, names)
        return None if base is None else _apply_segments(base, node.args)
    name = _func_name(func)
    if name in _PASSTHROUGH_FUNCS and node.args:
        base = _evaluate_ascent(node.args[0], names)
        if base is not None:
            if name in _JOINING_CTORS:
                return _apply_segments(base, node.args[1:])
            return base
    if isinstance(func, ast.Attribute):
        # A method on a rooted path (`.resolve()`, `.absolute()`, …) keeps it.
        base = _evaluate_ascent(func.value, names)
        if base is not None:
            return base
    # ⛔ Any other call that takes a rooted argument is assumed to hand it back
    # (the conservative reading: it keeps a climb inside visible rather than
    # letting an unmodelled wrapper launder it).
    for arg in node.args:
        base = _evaluate_ascent(arg, names)
        if base is not None:
            return base
    return None


_ASCENT_NODES = (ast.Attribute, ast.Subscript, ast.Call, ast.BinOp)


def _scan_depth(tree: ast.Module) -> "dict[int, _Ascent]":
    """Every line whose expression climbs past the image, with its worst ascent.

    Statements are visited IN ORDER per scope, so an assignment is known to
    the lines after it (``_THIS_DIR = os.path.dirname(__file__)`` then
    ``os.path.join(_THIS_DIR, "..", "..", "..")``). Functions are visited
    after their enclosing scope, seeded with that scope's final table —
    which is what they see when called.
    """
    worst: "dict[int, _Ascent]" = {}

    def emit(expr: ast.AST, names: dict) -> None:
        for n in ast.walk(expr):
            if not isinstance(n, _ASCENT_NODES):
                continue
            r = _evaluate_ascent(n, names)
            if r is None or not (r.unknown or r.peak > MAX_IMAGE_ANCESTOR_INDEX):
                continue
            prev = worst.get(n.lineno)
            if (prev is None or (r.unknown and not prev.unknown)
                    or (not prev.unknown and r.peak > prev.peak)):
                worst[n.lineno] = r

    def assign(stmt: ast.stmt, names: dict) -> None:
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            return
        r = _evaluate_ascent(value, names)
        for t in targets:
            if not isinstance(t, ast.Name):
                continue
            if r is None:
                names.pop(t.id, None)
            else:
                names[t.id] = r

    def visit(stmts, names: dict, funcs: list) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.append(stmt)
                continue
            if isinstance(stmt, ast.ClassDef):
                visit(stmt.body, dict(names), funcs)
                continue
            for _, value in ast.iter_fields(stmt):
                items = value if isinstance(value, list) else [value]
                if items and all(isinstance(i, ast.stmt) for i in items):
                    visit(items, names, funcs)
                    continue
                for item in items:
                    if isinstance(item, (ast.excepthandler, ast.match_case)):
                        visit(item.body, names, funcs)
                    elif isinstance(item, ast.withitem):
                        emit(item.context_expr, names)
                    elif isinstance(item, ast.expr):
                        emit(item, names)
            assign(stmt, names)

    module_names: dict = {}
    pending: list = []
    visit(tree.body, module_names, pending)
    scopes = [(fn, module_names) for fn in pending]
    while scopes:
        fn, outer = scopes.pop()
        local = dict(outer)
        inner: list = []
        visit(fn.body, local, inner)
        scopes.extend((f, local) for f in inner)
    return worst


def _ascent_kind(kinds: frozenset) -> str:
    """Which spelling produced the ascent: ``index`` / ``chain`` / ``mixed``.

    ⛔ The CONSEQUENCE differs by spelling, not by scope, and the failure
    message has to say which one: ``parents[N]`` past the end raises
    ``IndexError`` (loud, wherever it sits), while a ``.parent`` chain, nested
    ``dirname`` or a ``..`` segment SATURATES at the filesystem root and
    returns a wrong path with no exception (quiet). An earlier version of this
    message keyed the consequence off module-vs-function scope, which is
    unrelated: a ``parents[N]`` inside a function still raises.
    """
    if "index" in kinds and "chain" in kinds:
        return "mixed"
    return "index" if "index" in kinds else "chain"


def check_layout_depth_assumptions(
    tool_rel_paths: set, tools_src: Path = None
) -> list:
    """出貨檔不得靠「數上去幾層」定位 repo 內的東西（#1494、#1501、#1503）。

    映像把每支工具攤平（``/opt/da-tools/x.py``，3 個祖先），repo 佈局下同一支
    有 8~9 個。任何 ``__file__`` 起算、上溯超過
    :data:`MAX_IMAGE_ANCESTOR_INDEX` 層的寫法，在 repo 測試裡永遠是對的，在
    客戶手上的映像裡永遠是錯的 —— 這正是本 repo 全套 5000+ 測試看不到
    ``_grar_validate.py`` 那一行的原因。

    ⚠️ **這是 best-effort 的補刀，不是完整的類別守衛。** 它認得四種上溯
    （``parents[N]`` / ``.parent`` 鏈 / 巢狀 ``dirname`` / 字面 ``..`` 段），
    而**與拼法無關**的那一支是 ``tests/ops/test_image_flat_layout.py``（實際
    import、實跑 ``--generate-observed-map``，任何拼法都躲不掉）。本規則的價值
    在於補「安靜飽和」那一半，代價是它只覆蓋建模過的拼法。

    ⛔ 三輪對抗式盲審實測出來的七個盲點，逐條標明現況（issue 1503）：

    1. **已關** — ``parents[N]`` 記成 **N+1** 層（``parents[2]`` ≡
       ``.parent`` ×3）。``test_legal_shapes_stay_green`` 原本把
       ``parents[2]`` 釘成合法，那一格已移到報錯側。
    2. **已關** — 鏈式 ``parents[1].parents[1]`` 逐段累加（2+2=4 層）。
    3. **已關（範圍內）** — 同一 scope 內**依序**追蹤 ``NAME = <rooted 運算式>``
       賦值（module scope 與函式 scope 皆然，函式以外層 scope 的最終表為起點），
       所以 ``_THIS_DIR = os.path.dirname(__file__)`` 之後的
       ``os.path.join(_THIS_DIR, "..", "..", "..")`` 會被算成 4 層。
       **知情保留**：跨模組（``from x import _THIS_DIR``）、屬性
       （``self.root``）、函式參數、tuple 解構、``global`` 回寫、``AugAssign``
       不追——它們需要的是資料流分析，而本規則刻意維持單檔、單趟 AST。
    4. **已關（範圍內）** — 字面 ``".."`` / ``os.pardir`` 段在
       ``os.path.join`` / ``joinpath`` / ``Path(a, b, …)`` / ``p / ".."`` 中
       計為上溯，``"../.."`` 這類複合字串逐段拆開；一般名稱段計為下探，而
       ``peak``（途中爬到的最高點）才是判決依據。**知情保留**：非字面段
       （變數、``*parts``）計為 0——既不上也不下，刻意不往寬鬆方向花掉未知；
       字串拼接（``d + "/.."``、f-string）不計，出貨檔今天零處（改寫成
       ``os.path.join`` 即落回範圍內）。
    5. **知情保留** — ``Path(*p.parts[:-4])`` 這類切片不計數。要建模得模擬
       序列運算；今天出貨檔零處，觸發條件是第一處出現——屆時它仍逃不過
       ``test_image_flat_layout`` 的實跑，只是安靜的那一半要靠那支測試的
       具體斷言。
    6. **知情保留** — ``try/except IndexError`` 包起來的 ``parents[N]`` 仍會
       被報。捕到例外不代表退路正確（#1494 那種退到常數的退路正是 fail-open），
       而誤紅的代價是改寫成「找檔案」，那本來就是正解。
    7. **已關** — 隨第 1 條一起關：``parents[2]`` 與 ``.parent`` ×3 現在判決
       相同，把一種改寫成另一種不再是轉綠路。

    ⛔ **本規則在 CI 有兩個執行點**：``ci.yml`` Lint job 的
    ``pre-commit run build-completeness-check --all-files``（issue 1503 接上，
    先前 47 條 ``pre-commit run`` 裡沒有它），以及
    ``tests/lint/test_check_build_completeness.py`` 的 repo-level 迴歸
    （``test_actual_repo_has_no_depth_assumptions`` 與 ``TestRepoSmoke``，由
    ``python-tests-run`` 帶到）。後者仍是必要的：新規則若只加在 hook 側、
    twin 沒跟上，hook 的 ``files:`` 過濾可能讓它在 PR 上不觸發。

    Returns:
        list of (severity, message) tuples
    """
    tools_src = TOOLS_SRC if tools_src is None else tools_src
    errors = []
    for rel in sorted(tool_rel_paths):
        if not rel.endswith(".py"):
            continue
        src_path = tools_src / rel
        # ⛔ 「不存在」與「存在但驗不了」是兩個問題，這裡只管後者。
        # TOOL_FILES 指向不存在的檔案由 build.sh 自己 fail-fast
        # （`✗ Missing: … exit 1`），在這裡重複報會讓本規則對合成 fixture
        # 誤紅——而誤紅正是守衛被刪掉的原因。
        if not src_path.is_file():
            continue
        try:
            # utf-8-sig, not utf-8: a BOM is invisible to Python's own import
            # machinery but makes `ast.parse` raise on U+FEFF, so a perfectly
            # runnable tool (saved by a Windows editor, which this repo has)
            # would be reported as unverifiable.
            tree = ast.parse(src_path.read_text(encoding="utf-8-sig"))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            errors.append((
                "error",
                f"{rel}: 檔案在，但解析失敗（{type(exc).__name__}: {exc}），"
                f"無法確認它有沒有做佈局深度假設。"
            ))
            continue
        # innermost def/class per line → module scope is "no enclosing scope"
        enclosing = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                for ln in range(node.lineno,
                                (node.end_lineno or node.lineno) + 1):
                    enclosing.setdefault(ln, True)
        for lineno, r in sorted(_scan_depth(tree).items()):
            where = "函式內" if lineno in enclosing else "module scope"
            how = ("上溯層數不是字面常數，無法驗證"
                   if r.unknown else f"從 __file__ 上溯 {r.peak} 層")
            # ⛔ 後果由**拼法**決定，不是由 scope 決定。
            consequence = {
                "index": "超出範圍時 IndexError（module scope 就是 import 期死）",
                "chain": "不會拋錯，飽和在檔案系統根目錄——安靜地算出錯的路徑",
                "mixed": "視實際運算順序而定：可能 IndexError，也可能安靜飽和",
            }[_ascent_kind(r.kinds)]
            errors.append((
                "error",
                f"{rel}:{lineno} ({where}) {how}，"
                f"但映像攤平後只有 {MAX_IMAGE_ANCESTOR_INDEX + 1} 個祖先 → "
                f"{consequence}。改成「找檔案」而不是「數層數」"
                f"（見 _lib_compat.find_project_root）。"
            ))
    return errors


def check_required_data_files(
    build_tools: set, required: dict = None
) -> list:
    """REQUIRED_DATA_FILES 對照：模組出貨時其資料檔必須同船。

    Returns:
        list of (severity, message) tuples
    """
    required = REQUIRED_DATA_FILES if required is None else required
    errors = []
    for module, data_files in sorted(required.items()):
        if module not in build_tools:
            continue  # 模組本身沒出貨 → 資料檔要求不適用
        for data_file in data_files:
            if data_file not in build_tools:
                errors.append((
                    "error",
                    f"build.sh TOOL_FILES 有 '{module}' 但缺其資料檔"
                    f" '{data_file}'。Image 內該工具會靜默空轉"
                    f"（fail-quiet，比 crash 更難察覺）。"
                ))
    return errors


def format_text_report(errors: list, command_map: dict, build_tools: set) -> str:
    """格式化文字報告。"""
    lines = []
    lines.append("=" * 60)
    lines.append("build.sh ↔ COMMAND_MAP 雙向同步檢查")
    lines.append("=" * 60)
    lines.append(f"COMMAND_MAP: {len(command_map)} 命令")
    lines.append(f"build.sh TOOL_FILES: {len(build_tools)} 檔案")
    lines.append("")

    err_count = sum(1 for s, _ in errors if s == "error")
    warn_count = sum(1 for s, _ in errors if s == "warning")

    if not errors:
        lines.append("✓ 雙向同步完全一致，沒有遺漏。")
    else:
        for severity, msg in errors:
            prefix = "✗ ERROR" if severity == "error" else "⚠ WARNING"
            lines.append(f"  {prefix}: {msg}")
        lines.append("")
        lines.append(f"總計: {err_count} 錯誤, {warn_count} 警告")

    return "\n".join(lines)


def format_json_report(errors: list, command_map: dict, build_tools: set) -> str:
    """格式化 JSON 報告。"""
    return json.dumps({
        "check": "build-completeness",
        "command_map_count": len(command_map),
        "build_tools_count": len(build_tools),
        "errors": [{"severity": s, "message": m} for s, m in errors],
        "pass": not any(s == "error" for s, _ in errors),
    }, ensure_ascii=False, indent=2)


def main():
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="build.sh ↔ COMMAND_MAP 雙向同步檢查")
    parser.add_argument("--ci", action="store_true",
                        help="CI 模式: 有 error 時 exit 1")
    parser.add_argument("--json", action="store_true",
                        help="JSON 格式輸出")
    args = parser.parse_args()

    if not ENTRYPOINT_PATH.is_file():
        print(f"ERROR: entrypoint.py 不存在: {ENTRYPOINT_PATH}",
              file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    if not BUILD_SH_PATH.is_file():
        print(f"ERROR: build.sh 不存在: {BUILD_SH_PATH}",
              file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    command_map = parse_command_map(ENTRYPOINT_PATH)
    build_tools = parse_build_sh_tools(BUILD_SH_PATH)
    errors = check_append_form_arrays(BUILD_SH_PATH)
    errors += check_bidirectional(command_map, build_tools)
    tool_rel_paths = parse_build_sh_tool_paths(BUILD_SH_PATH)
    errors += check_underscore_imports(tool_rel_paths, build_tools)
    # ⛔ REQUIRED_DATA_FILES 比對的是「會不會一起進映像」，而 build.sh 有兩條
    # 搬運路徑（TOOL_FILES 走 scripts/tools、REPO_DATA_FILES 走 repo 樹）。只餵
    # 前者會讓一個確實有出貨的資料檔被報成缺漏（#1494）。刻意不併進
    # `build_tools` 本身 —— 那個集合還餵給雙向檢查，資料檔在那裡會變成孤兒警告。
    shipped_basenames = build_tools | {
        Path(p).name for p in parse_build_sh_repo_data_files(BUILD_SH_PATH)
    }
    errors += check_required_data_files(shipped_basenames)
    errors += check_layout_depth_assumptions(tool_rel_paths)

    if args.json:
        print(format_json_report(errors, command_map, build_tools))
    else:
        print(format_text_report(errors, command_map, build_tools))

    has_errors = any(s == "error" for s, _ in errors)
    if args.ci and has_errors:
        sys.exit(EXIT_VIOLATION)
    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
