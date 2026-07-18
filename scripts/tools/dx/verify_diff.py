#!/usr/bin/env python3
"""verify_diff.py — diff-scoped Python 測試選擇器（測試 ROI 第六輪 W6-E MVP）

給一組 changed files（`git diff --name-only` 輸出），選出「該跑哪些 Python
測試檔」並附理由。設計結論（方案 (a) 規則映射，owner 拍板）：

  1. **AST import 反查**（主力，~70.8% 覆蓋）：tests/ 下每個 test 檔的靜態
     import（含 importlib.import_module / __import__ 常數引數）反查到 repo
     內同名模組（conftest.py 的 sys.path 注入使測試以裸模組名 import）。
  2. **文字路徑掃描**（+動態載入/subprocess 路徑，回收到 ~86.2%）：test 原始
     碼中出現的 repo 路徑字串（scripts/…、rule-packs/…、helm/… 等）與少數
     特例 basename（Makefile / .pre-commit-config.yaml / tool-registry.yaml
     / mkdocs.yml）。
  3. **目錄規則 + 例外表**（剩餘 ~35 檔）：verify_diff_rules.yaml 的
     dir_rules / overrides / safe_ignore / unmapped_test_ok（全帶
     justification 欄位）。

保守規則（優先序）：
  - full_run_triggers（tests/conftest.py、tests/factories.py、pyproject.toml、
    scripts/tools/_lib_*.py 與各子目錄 `_` 開頭共用 lib）→ 全跑。
  - always_run：任何 scripts/tools/**/*.py 變更恆選 tests/shared 的
    cross-cutting sweep（exit-code / SAST / help / --json stdout 契約，
    這些 sweep 用 glob 收集所有工具）。
  - **fail-closed**：任一 changed file 映射不到且不在 safe_ignore → 警告 + 全跑。

Go 側不做選擇（W6-D 線：build cache 已增量）；本工具只處理 Python 測試選擇。
tenant-api Go 變更由 dir_rule 先攔到 tests/contract（make contract-test）。

映射檔 `verify_diff_map.json` 預生成進 repo（快速載入）；以 content-hash
（source_digest）做陳舊偵測，stale 時警告 + 現場重生（不落盤；要落盤用
--write-map）。

用法:
  git diff --name-only origin/main | python3 scripts/tools/dx/verify_diff.py --stdin
  python3 scripts/tools/dx/verify_diff.py --base origin/main --run
  python3 scripts/tools/dx/verify_diff.py scripts/tools/dx/bump_docs.py --dry-run
  python3 scripts/tools/dx/verify_diff.py --write-map     # 重生映射檔（進 repo）
  python3 scripts/tools/dx/verify_diff.py --check         # 映射保鮮 lint（Phase 2）

輸出:
  標準輸出: 選中測試清單 + 命中規則；--json 時為單一 JSON 文件
  標準錯誤: 警告（stale map / fail-closed）與進度訊息
  exit code: 0 = OK；1 = violation（--check 失敗、--run 測試紅、或有外部
  套件未跑且未帶 --ack-external）；2 = caller error

外部套件語義（外審 F4，選 fail-closed 方案）:
  Go / vitest / schemathesis 等非 pytest 套件本工具**不代跑**，只列出對應
  runner（如 make test-am-inhibit）。有外部套件受影響時預設 exit 1——
  「verify-diff 綠」不得被讀成「全部驗過」；呼叫端跑完（或有意識跳過）
  後帶 --ack-external 顯式確認才降回 exit 0。取捨：偏 fail-closed 而非
  「exit 0 + 欄位標注」，因為本工具的定位是 gate 前置，沉默的綠比吵鬧的
  紅危險；--json 亦帶 unrun_external 欄位供腳本判讀。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_compat import try_utf8_stdout  # noqa: E402
from _atomic_write import atomic_write_text  # noqa: E402

REPO_ROOT = Path(_THIS_DIR).resolve().parents[2]
DEFAULT_MAP_PATH = Path(_THIS_DIR) / "verify_diff_map.json"
DEFAULT_RULES_PATH = Path(_THIS_DIR) / "verify_diff_rules.yaml"

MAP_VERSION = 1

# 掃描時一律跳過的目錄名（fixture .py 是測試資料、不是可 import 的模組來源）
_SKIP_DIR_NAMES = {
    "__pycache__", "fixtures", "snapshots", "node_modules", ".git",
    "federation-e2e",  # 不在 make test 範圍（--ignore），選擇器不管它
}

# 文字掃描：test 原始碼中的 repo 路徑字串（頂層目錄白名單起頭、≥2 段）
_PATH_REF_RE = re.compile(
    r'(?:scripts|components|rule-packs|helm|k8s|try-local|tools|docs|policies|'
    r'operator-manifests|environments|tests|\.github)'
    r'/[A-Za-z0-9_\-][A-Za-z0-9_\-./]*'
)

# 特例 basename：測試以裸檔名引用的 repo 根部特殊檔（完整路徑掃描抓不到）。
# 刻意保持小集合——高頻檔（CHANGELOG.md / CLAUDE.md）會在 docstring 大量誤中，
# 不放進來；.md 類由 safe_ignore 收。
_SPECIAL_BASENAMES = {
    "Makefile": "Makefile",
    ".pre-commit-config.yaml": ".pre-commit-config.yaml",
    "tool-registry.yaml": "docs/assets/tool-registry.yaml",
    "mkdocs.yml": "mkdocs.yml",
}


# =============================================================================
# 小工具
# =============================================================================

def _posix(p: str) -> str:
    """路徑正規化為 forward-slash、去掉開頭 ./。"""
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def _glob_to_re(pattern: str) -> re.Pattern:
    """把 rules YAML 的 glob（支援 **）轉成全字串 regex。

    語意：`**` 跨層、`*` 不跨 `/`、`?` 單字元。純目錄 prefix 寫 `dir/**`。
    """
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
                # 吃掉 **/ 的斜線，讓 "a/**" 也能配到 "a" 本身之下第一層
                if i < len(pattern) and pattern[i] == "/":
                    i += 1
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _match_any(path: str, patterns) -> str | None:
    """回傳第一個命中的 pattern（原字串），沒中回 None。"""
    for pat in patterns:
        if _glob_to_re(pat).match(path):
            return pat
    return None


def _warn(msg: str) -> None:
    print(f"[verify-diff] {msg}", file=sys.stderr)


# =============================================================================
# 規則檔（YAML）載入與 schema 驗證
# =============================================================================

class RulesError(Exception):
    """rules YAML 缺欄位 / 格式錯誤（caller error）。"""


def _require(entry: dict, keys, where: str) -> None:
    if not isinstance(entry, dict):
        raise RulesError(f"{where}: 條目必須是 mapping，拿到 {type(entry).__name__}")
    missing = [k for k in keys if not entry.get(k)]
    if missing:
        raise RulesError(f"{where}: 缺必填欄位 {missing}（條目: {entry!r}）")


def load_rules(path: Path) -> dict:
    """載入並驗證 verify_diff_rules.yaml。schema 錯誤 → RulesError。"""
    import yaml
    if not path.exists():
        raise RulesError(f"rules 檔不存在: {path}")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise RulesError("rules YAML 頂層必須是 mapping")
    if data.get("version") != 1:
        raise RulesError(f"不支援的 rules version: {data.get('version')!r}")

    known = {"version", "always_run", "full_run_triggers", "dir_rules",
             "safe_ignore", "overrides", "unmapped_test_ok"}
    unknown = set(data) - known
    if unknown:
        raise RulesError(f"未知的 rules 欄位: {sorted(unknown)}")

    ar = data.get("always_run") or {}
    _require(ar, ["trigger", "tests"], "always_run")
    for e in ar["tests"]:
        _require(e, ["path", "justification"], "always_run.tests")

    for e in data.get("full_run_triggers") or []:
        _require(e, ["pattern", "justification"], "full_run_triggers")

    for e in data.get("dir_rules") or []:
        _require(e, ["name", "source", "suite", "runner", "justification"],
                 "dir_rules")

    for e in data.get("safe_ignore") or []:
        _require(e, ["pattern", "justification"], "safe_ignore")

    for e in data.get("overrides") or []:
        _require(e, ["source", "tests", "justification"], "overrides")

    for e in data.get("unmapped_test_ok") or []:
        _require(e, ["test", "justification"], "unmapped_test_ok")

    return data


# =============================================================================
# 映射建置（AST import 反查 + 文字掃描）
# =============================================================================

def _iter_py_files(root: Path):
    """走訪 root 下所有 .py（剪掉 _SKIP_DIR_NAMES 目錄）。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def git_tracked_paths(repo_root: Path):
    """git tracked 檔案集合 (files, dirs)；非 git 環境回 None。

    映射建置的存在性判定一律以此為準，不用檔案系統 .exists()——後者有
    平台相依性（PR #1156 CI 實證，F5 dict-compare 抓到）：
      - worktree 的 .git 是檔案、normal checkout 是目錄 → `ROOT/".git"/"HEAD"`
        類 joined-chain 兩邊收進不同的垃圾 entries；
      - Windows FS 大小寫不敏感 → "readme.md" 誤判存在（實檔 README.md）；
      - host 的 untracked 產物（bench-results、scratch 檔）只在本機存在。
    git tracked 集合跨平台決定性、且語義更正確（untracked 本就不該進映射）。

    回 None 的 fallback 僅供非 git 環境（單元測試的合成 repo、container 對
    worktree 的 Windows gitdir 邊角）——呼叫端會警告「非決定性」。
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"], capture_output=True,
            cwd=str(repo_root), timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    files = {f for f in proc.stdout.decode("utf-8", "replace").split("\0") if f}
    if not files:
        return None
    dirs: set = set()
    for f in files:
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    return files, dirs


def _is_repo_file(cand: str, repo_root: Path, tracked) -> bool:
    """cand 是否為 repo 檔案（tracked 集合優先；fallback 檔案系統）。"""
    if tracked is not None:
        return cand in tracked[0]
    return (repo_root / cand).is_file()


def _is_repo_path(cand: str, repo_root: Path, tracked) -> bool:
    """cand 是否為 repo 檔案或目錄（tracked 集合優先；fallback 檔案系統）。"""
    if tracked is not None:
        return cand in tracked[0] or cand in tracked[1]
    return (repo_root / cand).exists()


def build_module_index(repo_root: Path, tracked=None) -> dict:
    """裸模組名 → repo 內候選 .py 路徑（posix 相對路徑）list。

    來源：scripts/**（tools/ops/dx/lint + session-guards + ops）、
    components/da-tools/app、tests/**（非 test_ 的 helper，如 factories、
    _mutation_pilot、vm_harness、e2e-bench 的 aggregate/driver）。
    同名模組（罕見）→ 全列（保守：任一候選變更都選中 import 它的測試）。
    """
    index: dict = {}
    roots = [repo_root / "scripts", repo_root / "components" / "da-tools" / "app",
             repo_root / "tests"]
    for r in roots:
        if not r.is_dir():
            continue
        for py in _iter_py_files(r):
            rel = _posix(str(py.relative_to(repo_root)))
            if tracked is not None and rel not in tracked[0]:
                continue  # untracked .py 不進索引（決定性）
            index.setdefault(py.stem, []).append(rel)
    for k in index:
        index[k] = sorted(set(index[k]))
    return index


def _extract_imports(tree: ast.AST) -> set:
    """收集靜態 import 的頂層模組名 + importlib/__import__ 常數引數。"""
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            is_dyn = (
                (isinstance(func, ast.Attribute) and func.attr == "import_module")
                or (isinstance(func, ast.Name) and func.id == "__import__")
            )
            if is_dyn and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                names.add(node.args[0].value.split(".")[0])
    return names


def _extract_joined_path_refs(tree: ast.AST, repo_root: Path,
                              tracked=None) -> set:
    """段接式路徑（`ROOT / "scripts" / "tools" / "x.py"`、os.path.join(...)）。

    動態載入（spec_from_file_location / subprocess 以 Path 物件組路徑）常用
    這種寫法，純文字 regex 掃不到。只收「常數段 join 後存在於 repo 且是
    **檔案**」的結果——目錄不收，避免 tmp_path 假樹（tmp_path/"scripts"/
    "tools"）誤映射成真實目錄造成大面積過選。
    """
    refs: set = set()
    var_segs: dict = {}  # 模組層 `NAME = <chain>` 的常數段（輕量 dataflow）

    def _chain_consts(node: ast.AST) -> list:
        """左深 `/` chain 依序收常數字串段（非常數左根忽略）。

        chain 根若是已知模組層變數（`_DX_DIR = ROOT / "scripts" / ...`），
        展開其常數段——常見的兩層拆寫（先組目錄、再接檔名）。
        """
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            segs = _chain_consts(node.left)
            if isinstance(node.right, ast.Constant) and \
                    isinstance(node.right.value, str):
                segs.append(node.right.value)
            return segs
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.Name):
            return list(var_segs.get(node.id, []))
        return []

    # 先收模組層變數的常數段（只看 top-level Assign，夠用且不誤傷）
    for stmt in getattr(tree, "body", []):
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                and isinstance(stmt.targets[0], ast.Name):
            segs = _chain_consts(stmt.value)
            if segs:
                var_segs[stmt.targets[0].id] = segs

    def _try_add(segs: list) -> None:
        if not segs:
            return
        cand = _posix("/".join(segs))
        if cand and _is_repo_file(cand, repo_root, tracked):
            refs.add(cand)

    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            _try_add(_chain_consts(node))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "join":
                consts = [a.value for a in node.args
                          if isinstance(a, ast.Constant)
                          and isinstance(a.value, str)]
                _try_add(consts)
    return refs


def _extract_text_refs(source: str, repo_root: Path, tracked=None) -> set:
    """test 原始碼中的 repo 路徑字串（檔案或目錄，需為 repo tracked 路徑）。"""
    refs: set = set()
    for m in _PATH_REF_RE.finditer(source):
        cand = m.group(0).rstrip("./")
        if "/" not in cand:
            continue
        if _is_repo_path(cand, repo_root, tracked):
            refs.add(cand)
    for base, repo_path in _SPECIAL_BASENAMES.items():
        if base in source and _is_repo_file(repo_path, repo_root, tracked):
            refs.add(repo_path)
    return refs


def collect_test_files(repo_root: Path, tracked=None) -> list:
    """tests/ 下所有 pytest 可收集的 test_*.py（排除 federation-e2e / fixtures）。"""
    out = []
    tests_root = repo_root / "tests"
    for py in _iter_py_files(tests_root):
        if py.name.startswith("test_"):
            rel = _posix(str(py.relative_to(repo_root)))
            if tracked is not None and rel not in tracked[0]:
                continue  # untracked scratch test 不掃（決定性）
            out.append(rel)
    return sorted(out)


def compute_source_digest(repo_root: Path, module_index: dict,
                          tracked=None) -> str:
    """映射輸入的 content-hash：全部 test 檔內容 + 模組索引路徑清單。

    test 檔內容變 → import/文字掃描結果可能變；模組索引路徑集合變
    （工具改名/新增/移動）→ 反查結果可能變。兩者其一變即視為 stale。

    行尾正規化（CRLF→LF）後才 hash：Windows host working tree 是 CRLF、
    CI / dev container 是 LF——不正規化的話 host 產的映射檔在 CI 必被
    誤判 stale（test_repo_check_is_green 會假紅）。
    """
    h = hashlib.sha256()
    for rel in collect_test_files(repo_root, tracked):
        h.update(rel.encode("utf-8"))
        h.update((repo_root / rel).read_bytes().replace(b"\r\n", b"\n"))
    for name in sorted(module_index):
        for p in module_index[name]:
            h.update(p.encode("utf-8"))
    return h.hexdigest()


def build_map(repo_root: Path) -> dict:
    """全量建置映射（import_map / text_map / tests_scanned / digest）。

    存在性判定以 git tracked 集合為準（跨平台決定性）；非 git 環境警告後
    退回檔案系統判定（僅供合成測試 repo 等場景，產物不應 commit）。
    """
    tracked = git_tracked_paths(repo_root)
    if tracked is None:
        _warn("非 git 環境（git ls-files 不可用）——存在性判定退回檔案系統，"
              "建置結果不具跨平台決定性，產物不應 commit")
    module_index = build_module_index(repo_root, tracked)
    import_map: dict = {}
    text_map: dict = {}
    tests_scanned = collect_test_files(repo_root, tracked)
    parse_errors = []

    for rel in tests_scanned:
        src = (repo_root / rel).read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src, filename=rel)
        except SyntaxError:
            parse_errors.append(rel)
            continue
        for name in _extract_imports(tree):
            for source_path in module_index.get(name, []):
                if source_path == rel:
                    continue  # 自己 import 自己名（不會發生，防衛）
                import_map.setdefault(source_path, set()).add(rel)
        refs = _extract_text_refs(src, repo_root, tracked)
        refs |= _extract_joined_path_refs(tree, repo_root, tracked)
        for ref in refs:
            if ref == rel:
                continue
            text_map.setdefault(ref, set()).add(rel)

    return {
        "version": MAP_VERSION,
        "source_digest": compute_source_digest(repo_root, module_index, tracked),
        "import_map": {k: sorted(v) for k, v in sorted(import_map.items())},
        "text_map": {k: sorted(v) for k, v in sorted(text_map.items())},
        "tests_scanned": tests_scanned,
        "parse_errors": sorted(parse_errors),
    }


def write_map(vmap: dict, path: Path) -> None:
    """映射 JSON 落盤（atomic、LF、sorted → regen 冪等，diff 乾淨）。"""
    content = json.dumps(vmap, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    atomic_write_text(path, content, newline="\n")


def load_or_rebuild_map(repo_root: Path, map_path: Path) -> tuple:
    """載入映射檔；缺失或 stale → 警告 + 現場重生（不落盤）。

    Returns: (vmap, was_stale: bool)
    """
    on_disk = None
    if map_path.exists():
        try:
            with open(map_path, encoding="utf-8") as f:
                on_disk = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            _warn(f"映射檔損壞（{e}），現場重生")
            on_disk = None

    if on_disk is not None and on_disk.get("version") == MAP_VERSION:
        tracked = git_tracked_paths(repo_root)
        current_digest = compute_source_digest(
            repo_root, build_module_index(repo_root, tracked), tracked)
        if on_disk.get("source_digest") == current_digest:
            return on_disk, False
        _warn("映射檔陳舊（tests/ 或工具集已變），現場重生。"
              "建議跑 `--write-map` 更新進 repo。")
    elif on_disk is None and map_path == DEFAULT_MAP_PATH:
        _warn("映射檔不存在，現場重生。建議跑 `--write-map` 產生。")

    return build_map(repo_root), True


# =============================================================================
# 選擇引擎
# =============================================================================

FULL_SUITE_ARGS = ["tests/", "--ignore=tests/federation-e2e"]


def select_tests(changed: list, vmap: dict, rules: dict, repo_root: Path) -> dict:
    """核心選擇：changed files → 測試選集 + 理由。

    優先序（每個 changed file）：
      full_run_trigger → identity → overrides → import_map → text_map →
      dir_rules → （additive: always_run）→ safe_ignore → fail-closed。
    """
    selected: dict = {}      # test path → set(reasons)
    external: dict = {}      # suite → {"runner": str, "reasons": set}
    ignored: list = []       # {"path", "pattern", "justification"}
    unmapped: list = []      # fail-closed 來源
    full_reasons: list = []  # full_run_trigger 命中

    def add(test: str, reason: str) -> None:
        selected.setdefault(test, set()).add(reason)

    frt = rules.get("full_run_triggers") or []
    dir_rules = rules.get("dir_rules") or []
    overrides = rules.get("overrides") or []
    safe_ignore = rules.get("safe_ignore") or []
    ar = rules.get("always_run") or {}
    ar_trigger = ar.get("trigger")
    ar_tests = [e["path"] for e in (ar.get("tests") or [])]
    always_run_hit = False

    for raw in changed:
        f = _posix(raw)
        if not f:
            continue

        hit_full = next((e for e in frt
                         if _glob_to_re(e["pattern"]).match(f)), None)
        if hit_full:
            full_reasons.append(
                {"path": f, "pattern": hit_full["pattern"],
                 "justification": hit_full["justification"]})
            continue

        matched = False

        # identity：changed 的 test 檔自己一定要跑
        if (f.startswith("tests/") and f.endswith(".py")
                and Path(f).name.startswith("test_")
                and "/fixtures/" not in f
                and not f.startswith("tests/federation-e2e/")):
            add(f, f"identity: {f} 自身變更")
            matched = True

        for e in overrides:
            if _glob_to_re(e["source"]).match(f):
                for t in e["tests"]:
                    add(t, f"override: {e['source']}")
                matched = True

        if f in vmap.get("import_map", {}):
            for t in vmap["import_map"][f]:
                add(t, f"import: 測試 import 了 {f}")
            matched = True

        for ref, tests in vmap.get("text_map", {}).items():
            if f == ref or f.startswith(ref + "/"):
                for t in tests:
                    add(t, f"text-ref: 測試原始碼引用 {ref}")
                matched = True

        for e in dir_rules:
            if _glob_to_re(e["source"]).match(f):
                if e["runner"] == "pytest":
                    add(e["suite"], f"dir-rule:{e['name']} ({e['source']})")
                else:
                    ext = external.setdefault(
                        e["suite"], {"runner": e["runner"], "reasons": set()})
                    ext["reasons"].add(f"dir-rule:{e['name']} ← {f}")
                matched = True

        # additive：工具變更恆選 cross-cutting sweep（不算 matched——
        # 沒有專屬測試的新工具仍應 fail-closed 全跑）
        if ar_trigger and _glob_to_re(ar_trigger).match(f):
            always_run_hit = True

        if not matched:
            hit_ignore = next((e for e in safe_ignore
                               if _glob_to_re(e["pattern"]).match(f)), None)
            if hit_ignore:
                ignored.append({"path": f, "pattern": hit_ignore["pattern"],
                                "justification": hit_ignore["justification"]})
            else:
                unmapped.append(f)

    if always_run_hit:
        for t in ar_tests:
            add(t, "always-run: scripts/tools 工具變更 → cross-cutting sweep 恆選")

    # 去重：已被選中的目錄 suite 蓋掉其下的單檔（pytest 跑目錄即含檔）
    suite_dirs = [t for t in selected if not t.endswith(".py")]
    for t in list(selected):
        if t.endswith(".py"):
            for d in suite_dirs:
                if t.startswith(d.rstrip("/") + "/"):
                    selected[d] |= selected.pop(t)
                    break

    mode = "subset"
    if unmapped or full_reasons:
        mode = "full"
    elif not selected and not external:
        mode = "empty"

    return {
        "mode": mode,
        "changed_count": len([c for c in changed if _posix(c)]),
        "selected": {t: sorted(r) for t, r in sorted(selected.items())},
        "external_suites": {s: {"runner": v["runner"],
                                "reasons": sorted(v["reasons"])}
                            for s, v in sorted(external.items())},
        # F4：本工具不代跑的外部套件（Go/vitest/schemathesis）。非空時預設
        # exit 1，--ack-external 顯式確認後降 0；腳本端請判讀此欄位。
        "unrun_external": [{"suite": s, "runner": v["runner"]}
                           for s, v in sorted(external.items())],
        "ignored": ignored,
        "unmapped": sorted(unmapped),
        "full_run_triggers_hit": full_reasons,
    }


# =============================================================================
# 執行（--run）
# =============================================================================

def build_pytest_argv(result: dict, xdist_threshold: int) -> list:
    """由選擇結果組 pytest argv。小集 sequential（xdist 啟動 ~2s 反虧）。"""
    if result["mode"] == "full":
        return [sys.executable, "-m", "pytest", *FULL_SUITE_ARGS,
                "-n", "auto", "--tb=short"]
    targets = sorted(result["selected"])
    argv = [sys.executable, "-m", "pytest", *targets, "--tb=short"]
    if len(targets) > xdist_threshold:
        argv += ["-n", "auto"]
    return argv


def run_pytest(result: dict, xdist_threshold: int, timeout_s: int,
               repo_root: Path) -> int:
    """實跑選中的 pytest 集。回傳 exit code（0 綠、1 有紅）。"""
    if result["mode"] == "empty" and not result["external_suites"]:
        _warn("選集為空——本次 diff 無 Python 測試面，不跑 pytest。")
        return EXIT_OK
    for suite, info in result["external_suites"].items():
        _warn(f"外部套件 {suite} 受影響 → 請另跑: {info['runner']}"
              f"（非 pytest 收集，本工具不代跑）")
    if result["mode"] == "empty":
        return EXIT_OK

    argv = build_pytest_argv(result, xdist_threshold)
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")  # zh-TW Windows cp950 防線
    _warn("執行: " + " ".join(argv[2:]))
    proc = subprocess.run(argv, cwd=str(repo_root), env=env,
                          timeout=timeout_s)
    return EXIT_OK if proc.returncode == 0 else EXIT_VIOLATION


# =============================================================================
# 映射保鮮 lint（--check，Phase 2）
# =============================================================================

def check_map(repo_root: Path, map_path: Path, rules: dict) -> tuple:
    """--check：(1) 落盤映射檔須新鮮；(2) 每個 test 檔須有映射路徑或例外。

    「有映射路徑」= 出現在 import_map/text_map 的 value、位於某 dir_rule 的
    suite 之下、或屬 always_run 集。identity（自身變更）不算——那對「改到
    source 時會不會選中它」沒有貢獻。

    Returns: (problems: list[str], fresh_map: dict)
    """
    problems: list = []
    fresh = build_map(repo_root)

    if not map_path.exists():
        problems.append(f"映射檔缺失: {map_path.name}（跑 --write-map 產生）")
    else:
        try:
            with open(map_path, encoding="utf-8") as f:
                on_disk = json.load(f)
            if on_disk.get("source_digest") != fresh["source_digest"]:
                problems.append(
                    f"映射檔陳舊: {map_path.name} 的 source_digest 與現況不符"
                    "（跑 --write-map 更新）")
            else:
                # F5：digest 同不代表內容同——手改 map、舊版工具產出、或
                # digest 未覆蓋的輸入（fixture 後補）都可能讓 committed map
                # 缺 ref 卻「永遠新鮮」。fresh 已經 build 好，dict 相等比對
                # 成本近零，直接收掉這個洞。
                for key in ("import_map", "text_map", "tests_scanned"):
                    if on_disk.get(key) != fresh[key]:
                        problems.append(
                            f"映射內容不符: {map_path.name} 的 {key} 與現場重建"
                            "結果不同（digest 相同仍不符＝map 被手改或由不同"
                            "版本工具產生；跑 --write-map 重生）")
        except (json.JSONDecodeError, OSError) as e:
            problems.append(f"映射檔無法解析: {e}（跑 --write-map 重生）")

    covered: set = set()
    for tests in fresh["import_map"].values():
        covered.update(tests)
    for tests in fresh["text_map"].values():
        covered.update(tests)
    ar_tests = {e["path"] for e in (rules.get("always_run") or {}).get("tests", [])}
    covered |= ar_tests
    suite_prefixes = [e["suite"].rstrip("/") + "/"
                      for e in rules.get("dir_rules") or []
                      if e["runner"] == "pytest"]
    override_tests: set = set()
    for e in rules.get("overrides") or []:
        override_tests.update(e["tests"])
    covered |= override_tests
    exempt = {e["test"] for e in rules.get("unmapped_test_ok") or []}

    for rel in fresh["tests_scanned"]:
        if rel in covered or rel in exempt:
            continue
        if any(rel.startswith(p) for p in suite_prefixes):
            continue
        problems.append(
            f"未映射 test 檔: {rel} —— import/文字掃描都解析不到、無 dir_rule "
            "覆蓋、也不在 unmapped_test_ok 例外表（加映射線索或帶 "
            "justification 入例外表）")

    stale_exempt = exempt - set(fresh["tests_scanned"])
    for t in sorted(stale_exempt):
        problems.append(f"例外表殭屍條目: {t} 已不存在（unmapped_test_ok 應清理）")

    return problems, fresh


# =============================================================================
# 報告輸出
# =============================================================================

def format_text_report(result: dict) -> str:
    lines = []
    lines.append("=" * 70)
    lines.append("verify-diff — diff-scoped Python 測試選擇")
    lines.append("=" * 70)
    lines.append(f"變更檔數: {result['changed_count']}    模式: {result['mode']}")
    lines.append("")

    if result["full_run_triggers_hit"]:
        lines.append("⛔ 全跑觸發（保守規則）")
        for e in result["full_run_triggers_hit"]:
            lines.append(f"  {e['path']}  [{e['pattern']}] — {e['justification']}")
        lines.append("")
    if result["unmapped"]:
        lines.append("⚠ fail-closed：以下變更映射不到 → 全跑")
        for p in result["unmapped"]:
            lines.append(f"  {p}")
        lines.append("")

    if result["mode"] == "full":
        lines.append("→ 建議執行: pytest " + " ".join(FULL_SUITE_ARGS) + " -n auto")
    elif result["selected"]:
        lines.append(f"✓ 選中 pytest 目標 ({len(result['selected'])})")
        for t, reasons in result["selected"].items():
            lines.append(f"  {t}")
            for r in reasons:
                lines.append(f"    · {r}")
    if result["external_suites"]:
        lines.append("")
        lines.append("◇ 受影響的外部套件（非 pytest，本工具不代跑）")
        for s, info in result["external_suites"].items():
            lines.append(f"  {s} → {info['runner']}")
            for r in info["reasons"]:
                lines.append(f"    · {r}")
    if result["ignored"]:
        lines.append("")
        lines.append(f"· safe-ignore ({len(result['ignored'])})")
        for e in result["ignored"]:
            lines.append(f"  {e['path']}  [{e['pattern']}]")
    if result["mode"] == "empty":
        lines.append("")
        lines.append("（無 Python 測試面）")
    if result["unrun_external"]:
        lines.append("")
        lines.append(f"⚠ {len(result['unrun_external'])} 個外部套件未跑"
                     "（本工具不代跑；跑完或有意識跳過後帶 --ack-external "
                     "確認，否則 exit 1）:")
        for e in result["unrun_external"]:
            lines.append(f"  {e['suite']} → {e['runner']}")
    lines.append("=" * 70)
    return "\n".join(lines)


# =============================================================================
# Main
# =============================================================================

def _read_changed_files(args, repo_root: Path) -> list:
    if args.stdin:
        return [ln.strip() for ln in sys.stdin if ln.strip()]
    if args.base:
        proc = subprocess.run(
            ["git", "diff", "--name-only", args.base],
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(repo_root), timeout=60,
        )
        if proc.returncode != 0:
            print(f"Error: git diff --name-only {args.base} 失敗: "
                  f"{proc.stderr.strip()}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
        return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    return list(args.files)


def main() -> None:
    """CLI entry point: diff-scoped Python 測試選擇器（W6-E）。"""
    try_utf8_stdout()  # 舊式 Windows console（cp950 等）防 UnicodeEncodeError
    parser = argparse.ArgumentParser(
        description="由 changed files 選出該跑的 Python 測試（規則映射 + fail-closed）")
    parser.add_argument("files", nargs="*",
                        help="changed files（或用 --stdin / --base）")
    parser.add_argument("--stdin", action="store_true",
                        help="從 stdin 讀 changed files（git diff --name-only | ...）")
    parser.add_argument("--base", metavar="REF",
                        help="git diff --name-only REF 取 changed files")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列選集（預設行為；此旗標為顯式語氣）")
    parser.add_argument("--run", action="store_true",
                        help="直接執行選中的 pytest 集")
    parser.add_argument("--ack-external", action="store_true",
                        help="顯式確認外部套件（Go/vitest/contract）已另行處理；"
                             "未帶時只要有外部套件受影響即 exit 1（fail-closed）")
    parser.add_argument("--json", action="store_true",
                        help="stdout 輸出單一 JSON 文件")
    parser.add_argument("--check", action="store_true",
                        help="映射保鮮 lint：映射檔 stale 或有未映射 test 檔 → exit 1")
    parser.add_argument("--write-map", action="store_true",
                        help="重生映射檔並寫入 repo（進 commit）")
    parser.add_argument("--map", default=str(DEFAULT_MAP_PATH),
                        help="映射 JSON 路徑（預設 scripts/tools/dx/verify_diff_map.json）")
    parser.add_argument("--rules", default=str(DEFAULT_RULES_PATH),
                        help="規則 YAML 路徑（預設 scripts/tools/dx/verify_diff_rules.yaml）")
    parser.add_argument("--repo-root", default=str(REPO_ROOT),
                        help=argparse.SUPPRESS)  # 測試注入 seam
    parser.add_argument("--xdist-threshold", type=int, default=10,
                        help="選中檔數超過此值改用 -n auto（預設 10；小集 sequential）")
    parser.add_argument("--pytest-timeout", type=int, default=7200,
                        help="--run 的 pytest 子行程 timeout 秒數（預設 7200）")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    map_path = Path(args.map)
    rules_path = Path(args.rules)

    try:
        rules = load_rules(rules_path)
    except RulesError as e:
        print(f"Error: rules 檔驗證失敗: {e}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    if args.write_map:
        vmap = build_map(repo_root)
        write_map(vmap, map_path)
        _warn(f"映射檔已更新: {map_path}（{len(vmap['tests_scanned'])} 個 test 檔、"
              f"import_map {len(vmap['import_map'])} 條、"
              f"text_map {len(vmap['text_map'])} 條）")
        sys.exit(EXIT_OK)

    if args.check:
        problems, fresh = check_map(repo_root, map_path, rules)
        payload = {"ok": not problems, "problems": problems,
                   "tests_scanned": len(fresh["tests_scanned"])}
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            if problems:
                print(f"✗ verify-diff --check 失敗（{len(problems)} 個問題）:")
                for p in problems:
                    print(f"  - {p}")
            else:
                print(f"✓ 映射新鮮且 {len(fresh['tests_scanned'])} 個 test 檔全數"
                      "可達（import/text/dir-rule/always-run/例外表）")
        sys.exit(EXIT_VIOLATION if problems else EXIT_OK)

    changed = _read_changed_files(args, repo_root)
    if not changed:
        print("Error: 沒有 changed files（給定位置引數、--stdin 或 --base REF）",
              file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    vmap, _stale = load_or_rebuild_map(repo_root, map_path)
    result = select_tests(changed, vmap, rules, repo_root)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_text_report(result))

    # F4 fail-closed：外部套件未跑且未顯式確認 → exit 1（--run 與列表模式
    # 一致，避免「verify-diff 綠」被讀成「全部驗過」）。
    unacked_external = bool(result["unrun_external"]) and not args.ack_external

    if args.run:
        rc = run_pytest(result, args.xdist_threshold,
                        args.pytest_timeout, repo_root)
        if rc == EXIT_OK and unacked_external:
            _warn(f"{len(result['unrun_external'])} 個外部套件未跑且未 "
                  "--ack-external → exit 1")
            rc = EXIT_VIOLATION
        sys.exit(rc)

    if unacked_external:
        _warn(f"{len(result['unrun_external'])} 個外部套件未跑且未 "
              "--ack-external → exit 1")
        sys.exit(EXIT_VIOLATION)
    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
