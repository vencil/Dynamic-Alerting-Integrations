"""`--dry-run` zero-side-effect contract gate for da-tools CLI tools (ops / dx / lint).

THE CONTRACT
------------
A tool invoked with ``--dry-run`` promises a PREVIEW: it may print what it
*would* do, but it must not create, modify, or delete a single file.  (E.g.
``init_project --dry-run`` self-describes as「顯示會產生的檔案但不寫入」.)

The observable form of the contract: every recipe runs inside an isolated
tmp tree (input fixtures COPIED under it, subprocess ``cwd`` under it, every
declared output path under it).  A ``(relative-path, sha256)`` snapshot of
the WHOLE tmp tree is taken before and after the run, and the two snapshots
must be set-equal — nothing added, nothing removed, nothing rewritten.
Directories count too: even ``mkdir``-ing an empty output directory is a
side effect a "preview" has no business performing.

A vacuous pass is also guarded against: each recipe must exit with one of
its expected graceful codes (0 for most; ``generate_rule_pack_split`` also
gracefully exits 1 = EXIT_VIOLATION on its pre-existing in-repo metric
mismatch report), and a non-zero graceful exit must still have produced its
report on stdout.  A recipe that crashes before doing any work would
trivially leave the tree unchanged; these two guards prove the tool actually
walked its dry-run path.

WHY THIS FILE EXISTS
--------------------
~18 ops tools advertise ``--dry-run``, and until this gate NOTHING in the
repo asserted the filesystem actually stays untouched.  (Scope widened to
``dx/`` and ``lint/`` in #1454 D — see §6 below.)  This is the #1112
lesson extended from stdout identity to *filesystem* identity: "the preview
parsed fine" proves nothing about what it silently wrote.  A dry-run-writes
regression fires exactly when a customer is "just previewing" against their
conf.d/ — high consequence, near-zero detection.

SCOPE — WHAT COUNTS AS A --dry-run TOOL
---------------------------------------
Scope discovery is argparse-based: a tool under ``scripts/tools/`` (the root
and EVERY subdirectory holding ``*.py``, derived from the tree — not a
hard-coded list) is in scope iff it *declares* ``add_argument("--dry-run")``
(either quote style).  ``test_recipe_table_covers_every_dry_run_tool`` fails
if a tool grows the flag without gaining a recipe;
``test_dry_run_tool_count_per_dir_is_pinned`` pins ops 18 / dx 15 / lint 1;
``test_no_dry_run_declarer_outside_the_gate`` scans the WHOLE repo, so the
denominator is not a function of the directories this gate chose to walk.

Three files mention ``--dry-run`` without declaring it, and are held in an
explicit, meta-asserted allowlist (``MENTION_ONLY_ALLOWLIST``):

* ``batchpr_dispatch`` — pure forwarder; the flag belongs to the ``da-batchpr``
  Go binary (not buildable/hermetic from this Python gate; covered Go-side).
* ``drift_detect`` — docstring prose only; the tool has no ``--dry-run`` flag.
* ``threshold_govern`` — dry-run is the *default* (absence of ``--apply``),
  not a flag; its no-write default is exercised by its own test suite.

SCOPE — HONEST BOUNDARIES
-------------------------
1. **Only the tmp tree is watched.**  A tool that writes OUTSIDE it (system
   temp, the repo tree, ``$HOME`` caches) is not caught by this gate.  Repo-
   tree writes have their own guard (`threshold_recommend`'s observed-map
   writer runs sandboxed in test_json_stdout_contract.py); the rest is an
   accepted limit, not an oversight.
2. **One recipe per tool, one subprocess per tool** — runtime discipline.
   18 subprocess launches ≈ tens of seconds total; a (tool × mode) matrix
   like the --json gate's 84 recipes would triple that for little marginal
   signal, because the dry-run *branch* is what's under test, not every
   mode combination around it.  ⚠️ 例外（``MULTI_VARIANT_TOOLS``，附理由）：
   一次呼叫走不到全部寫入守衛的工具，一個守衛分支一個具名 variant——
   ``analyze_bench_history`` ×5、``bump_docs`` ×2。判準是「拿掉任一個守衛，
   都有一條會紅」，不是「每支都有一條 recipe」。
3. **Legitimate writes are excluded by recipe design, not by exemption:**
   no recipe passes a report/output-writing flag alongside ``--dry-run``
   (e.g. an explicit ``--report-file``), so any write observed is a
   violation by construction.  ⚠️ 反過來，**開啟寫入模式**的旗標（
   ``--execute`` / ``--apply`` / ``--update`` / ``--output``）在需要時是
   刻意跟 ``--dry-run`` 同給的：寫入要那個旗標才開的工具，只給 ``--dry-run``
   的話守衛根本擋不到東西，拿掉它也不會紅。
4. **No recipe needs kubectl or a real network** — the three recipes that
   talk HTTP get the same local stub server the --json gate uses, so this
   gate runs fully (no skips) on Windows hosts as well as POSIX CI.
5. **Data-shape coverage is one-deep** (same accepted limit as the --json
   gate): each dry-run path is driven with one upstream payload shape.
   Probed at landing time: even against the empty-stub payloads the
   report-heavy tools (e.g. ``threshold_recommend``) still walk their full
   per-tenant loop and emit a complete report, so the would-write machinery
   is genuinely exercised — but a write hidden behind a specific data shape
   no recipe produces would not be caught.
6. **dx/ 與 lint/ 已納入（#1454 D）。** 先前這裡寫「Scope is ops/ only」，
   並把 dx 數成 14——實際是 dx 15 ＋ lint 1，而原本數洞的那條測試只數 dx，
   ``lint/fix_doc_links.py`` 連洞都沒被數到。那些工具的 REPO_ROOT 多半由
   ``__file__`` 推導，所以 recipe 用 ``sandbox``：把 ``scripts/tools/`` 與
   所需輸入複製成 ``tmp/repo``，從副本執行，寫入才會落在快照看得到的地方。
   ``analyze_bench_history`` 的寫入是 ``gh`` 呼叫而不是檔案：假 ``gh``
   把寫入記進 tmp tree 的一個檔（POSIX only，同 dx --json gate 的邊界）。
   ⚠️ 仍不在範圍內：``verify_diff --run``（真的跑 pytest）與
   ``migrate_ssot_language --git``（``git mv``）——這兩條路徑不是守衛擋的。
7. **非空綠的常設證明** — dx/lint 的 recipe 帶 ``write_control``：
   ``test_write_mode_control`` 用同一份 fixture、拿掉 ``--dry-run`` 再跑，
   要求 tree **有**變。落地時它就抓到一條空的綠（``add_frontmatter`` 的
   fixture 放錯層，寫入模式什麼都沒收）。ops 的 18 條沒有這條對照
   （落地時只對其中三支手動量過）——這是已知的缺口，不是已覆蓋。

FIRST-RUN REDS → KNOWN_DRY_RUN_WRITERS
--------------------------------------
A tool caught writing in dry-run is a REAL BUG CANDIDATE, and fixing it is
a behaviour change outside this gate's remit.  Such tools are listed in
``KNOWN_DRY_RUN_WRITERS`` (value = what it wrote + one-line symptom) and
xfail(strict=True): the gate documents the violation today and goes red the
moment someone fixes the tool without delisting it.

HARNESS REUSE
-------------
The HTTP stub handler and the readiness-JSON builder are imported from
``test_json_stdout_contract`` (same directory, so pytest's prepend import
mode resolves it) — zero changes to that gate.  Fixture *copies* rather
than in-repo paths are deliberate: pointing a tool at the real
``try-local/seed/conf.d`` would leave input mutations invisible to the
snapshot.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import pytest

import test_json_stdout_contract as _jsc

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT / "scripts" / "tools"
OPS_DIR = TOOLS_ROOT / "ops"

# ⛔ 掃描範圍是 `scripts/tools/` 底下**每一個**放 CLI 工具的子目錄，由目錄樹
# 推導而不是列舉（#1454 D）。這個閘門先前只走 `OPS_DIR`，於是 `dx/` 的 15 支
# 與 `lint/` 的 1 支從未被檢查過——沒有失敗、沒有 skip、也沒有任何一行提到。
# 寫死成 ("ops", "dx", "lint") 會在下一個子目錄出現時重演同一個洞。
_NOT_TOOL_DIRS = frozenset({"__pycache__"})


def _tool_dirs() -> list[Path]:
    """`scripts/tools/` itself plus every subdirectory that holds `*.py`."""
    subdirs = sorted(
        d for d in TOOLS_ROOT.iterdir()
        if d.is_dir() and d.name not in _NOT_TOOL_DIRS and any(d.glob("*.py"))
    )
    return [TOOLS_ROOT, *subdirs]

# Real in-repo fixtures — COPIED into the tmp tree per recipe (never
# referenced in place: input mutation must land inside the snapshot).
SEED_CONF_D = REPO_ROOT / "try-local" / "seed" / "conf.d"
RULE_PACKS = REPO_ROOT / "rule-packs"
K8S_MONITORING = REPO_ROOT / "k8s" / "03-monitoring"
DASHBOARD_JSON = K8S_MONITORING / "shadow-monitoring-dashboard.json"
ALERTMANAGER_YML = REPO_ROOT / "try-local" / "alertmanager.yml"

FIXTURE_PATHS = [SEED_CONF_D, RULE_PACKS, K8S_MONITORING, DASHBOARD_JSON,
                 ALERTMANAGER_YML]

TIMEOUT_S = 90

# ═══════════════════════════════════════════════════════════════════════════
# Scope discovery — argparse-declared --dry-run flags only
# ═══════════════════════════════════════════════════════════════════════════
# Both quote styles occur in the tree (init_project and
# generate_tenant_mapping_rules use single quotes); \s* spans the newline in
# the multi-line add_argument( \n "--dry-run" ) formatting.
DRY_RUN_FLAG_RE = re.compile(r"""add_argument\(\s*['"]--dry-run['"]""")
DRY_RUN_MENTION_RE = re.compile(r"--dry-run")

# Files that MENTION --dry-run without DECLARING it (see module docstring).
# Meta-asserted: an entry that stops mentioning the flag, or starts declaring
# it, fails test_mention_only_allowlist_is_exact.
MENTION_ONLY_ALLOWLIST: dict[str, str] = {
    "batchpr_dispatch": "forwarder — the flag lives in the da-batchpr Go binary",
    "drift_detect": "docstring prose only; no --dry-run flag declared",
    "threshold_govern": "dry-run is the default mode (no --apply), not a flag",
}


def _tool_files() -> list[Path]:
    return [
        f
        for d in _tool_dirs()
        for f in sorted(d.glob("*.py"))
        if not f.name.startswith("_") and f.name != "__init__.py"
    ]


def _tool_sources() -> dict[str, str]:
    return {f.stem: f.read_text(encoding="utf-8") for f in _tool_files()}


def collect_dry_run_tools() -> list[str]:
    """Every da-tools CLI tool that argparse-declares a `--dry-run` flag."""
    return [name for name, src in _tool_sources().items()
            if DRY_RUN_FLAG_RE.search(src)]


DRY_RUN_TOOLS = collect_dry_run_tools()

# tool 名稱 → 它在 repo 裡的那支腳本。名稱以 stem 為鍵，所以跨子目錄同名會讓
# 其中一支靜默蓋掉另一支；`test_tool_names_are_unique_across_dirs` 擋這件事。
TOOL_SCRIPTS: dict[str, Path] = {f.stem: f for f in _tool_files()}


# ═══════════════════════════════════════════════════════════════════════════
# Known violators — dry-run writes observed at gate-landing time (2026-07).
# Listed, NOT fixed: the fix is a behaviour change for the owner to schedule.
# Keys are tool names; presence ⇒ that tool's recipe is xfail(strict=True).
# ═══════════════════════════════════════════════════════════════════════════
KNOWN_DRY_RUN_WRITERS: dict[str, str] = {
    # EMPTY at gate-landing time (2026-07-17): the first full run found all 18
    # in-scope dry-run paths filesystem-clean, and a mutation check of the
    # harness itself (same recipes with --dry-run dropped) confirmed the
    # snapshot diff DOES detect write-mode output (33/4/8 added entries for
    # generate_rule_pack_split / onboard_platform / migrate_rule), so the
    # green is meaningful, not vacuous.
    #
    # 擴建到 dx/ 時（#1454 D）量到兩支：`inject_related_docs --update --dry-run`
    # 與 `migrate_conf_d --apply --dry-run` 都照寫——`--dry-run` 從來沒被讀過。
    # 兩支已改成 dry-run 優先（issue #1454 後續），清單回到空的。
}


# ═══════════════════════════════════════════════════════════════════════════
# Tmp-tree helpers
# ═══════════════════════════════════════════════════════════════════════════
def _tree_snapshot(root: Path) -> dict[str, str]:
    """(relative-posix-path → sha256 | "<dir>") for every entry under root."""
    snap: dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        if d != root:
            snap[d.relative_to(root).as_posix() + "/"] = "<dir>"
        for fn in filenames:
            p = d / fn
            snap[p.relative_to(root).as_posix()] = hashlib.sha256(
                p.read_bytes()).hexdigest()
    return snap


def _copy_into(tmp: Path, source: Path, name: str) -> str:
    """Copy a repo fixture (file or dir) under tmp/fix/<name>; return the copy."""
    dest = tmp / "fix" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, dest)
    else:
        shutil.copy2(source, dest)
    return str(dest)


def _out(tmp: Path, name: str) -> str:
    """A declared output path under tmp/out — deliberately NOT created.

    Dry-run must not even mkdir it; if the tool does, the snapshot diff
    reports `out/<name>/` as an added entry.
    """
    return str(tmp / "out" / name)


def _existing_dir(tmp: Path, name: str) -> str:
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _write(tmp: Path, name: str, text: str) -> str:
    p = tmp / "fix" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return str(p)


def _threshold_cr(tmp: Path) -> str:
    """Minimal ThresholdConfig CR for da_assembler --render-cr (offline)."""
    return _write(tmp, "tc.yaml", (
        "apiVersion: dynamicalerting.io/v1alpha1\n"
        "kind: ThresholdConfig\n"
        "metadata:\n"
        "  name: tenant-one\n"
        "  namespace: monitoring\n"
        "spec:\n"
        "  tenants:\n"
        "    tenant-one:\n"
        "      max_connections: '100'\n"
    ))


def _legacy_rules(tmp: Path) -> str:
    """Minimal legacy Prometheus rules file for migrate_rule."""
    return _write(tmp, "legacy-rules.yaml", (
        "groups:\n"
        "  - name: legacy\n"
        "    rules:\n"
        "      - alert: HighConnectionCount\n"
        "        expr: mysql_global_status_threads_connected > 100\n"
        "        for: 5m\n"
        "        labels:\n"
        "          severity: warning\n"
        "        annotations:\n"
        "          summary: connection count high\n"
    ))


# ═══════════════════════════════════════════════════════════════════════════
# dx / lint 擴建用的 fixture 工具（#1454 D）
# ═══════════════════════════════════════════════════════════════════════════
def _make_sandbox(tmp: Path, rels: tuple[str, ...]) -> Path:
    """`tmp/repo` = `scripts/tools/` 的副本 + 指名的 repo 相對路徑。

    ⚠️ 輸入是**複製**不是 symlink：`_tree_snapshot` 的 `os.walk` 不跟隨目錄
    symlink，寫進 symlink 目錄的東西會落回真的 repo，而且快照看不到。
    """
    root = tmp / "repo"
    shutil.copytree(TOOLS_ROOT, root / "scripts" / "tools",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for rel in rels:
        src, dst = REPO_ROOT / rel, root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, dst)
    return root


def _sb(tmp: Path, rel: str) -> Path:
    return tmp / "repo" / rel


def _edit(path: Path, old: str, new: str) -> None:
    """就地改一個 fixture 檔；`old` 不在檔內就 fail，免得「沒改到」冒充「改了」。"""
    text = path.read_text(encoding="utf-8")
    assert old in text, f"fixture 前提不成立：{path} 裡沒有 {old!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


# git 身分與設定隔離：fixture 建 repo 與工具子行程共用。GIT_OPTIONAL_LOCKS=0
# 讓 `git status` 不去順手刷新 .git/index——那是 git 自己的快取寫入，不是工具
# 的副作用；不關掉的話，快照會把它算成 dry-run 的寫入。
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "dry-run-gate", "GIT_AUTHOR_EMAIL": "gate@example.invalid",
    "GIT_COMMITTER_NAME": "dry-run-gate",
    "GIT_COMMITTER_EMAIL": "gate@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
}


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
         "-c", "init.defaultBranch=main", *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=60,
        env={**os.environ, **_GIT_ENV},
    )
    assert proc.returncode == 0, f"fixture git {args} failed: {proc.stderr}"
    return proc.stdout.strip()


def _git_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture: initial")
    return root


# ── analyze_bench_history：假 `gh` ─────────────────────────────────────────
# 這支工具的寫入**不是檔案**，是 `gh issue edit/create/close/comment` 與
# `gh label create`。假 `gh` 把每一個寫入呼叫記進 tmp tree 裡的
# `gh-writes.log`——於是「dry-run 呼叫了 gh 寫入」在快照裡就是一個新增的檔案，
# 跟其他工具用同一個判準。讀取呼叫（run list / issue list）從 fixture 回應。
# ⚠️ POSIX only：Windows 上 subprocess 解析裸 `gh` 會找 gh.exe、無視 PATHEXT，
# shebang shim 會被略過而跑到真的 gh（同 tests/dx/test_dx_json_stdout_contract.py
# 的 fake_gh 邊界）。
_FAKE_GH = """#!{python}
import json, os, sys
a = sys.argv[1:]
d = os.environ["FAKE_GH_DIR"]
if a[:2] == ["run", "list"]:
    sys.stdout.write(open(os.path.join(d, "runs.json"), encoding="utf-8").read())
    sys.exit(0)
if a[:2] == ["issue", "list"]:
    sys.stdout.write(open(os.path.join(d, "issues.json"), encoding="utf-8").read())
    sys.exit(0)
if a[:1] in (["issue"], ["label"]):
    with open(os.environ["FAKE_GH_WRITE_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(a) + "\\n")
    sys.exit(0)
sys.stderr.write("fake gh: unhandled call: " + " ".join(a) + "\\n")
sys.exit(1)
"""

_BENCH = "BenchmarkDryRunProbe"
_BENCH_CANARY = "BenchmarkControlCanaryCPU"   # = analyze_bench_history.CANARY_BENCH
_BENCH_CPU = "Dry Run Probe CPU @ 1.00GHz"


def _bench_env(tmp: Path) -> dict[str, str]:
    bindir = tmp / "bin"
    return {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_GH_DIR": str(tmp / "fix" / "gh"),
        "FAKE_GH_WRITE_LOG": str(tmp / "gh-writes.log"),
    }


def _bench_history(tmp: Path, *, regress: bool, tonight_cpu: bool = True,
                   issue_body: str | None = None) -> list[str]:
    """十個 nightly run 的 artifact 快取 + 假 gh 的讀取回應；回傳 argv。

    快取目錄預先放好 `bench-baseline.txt` 與 `.download-complete`，工具走
    cache hit、不呼叫 `gh run download`（見 `download_artifact`）。
    `regress=True` ⇒ 最近三晚 +11%（高於 5% floor）⇒ 有 finding。
    `tonight_cpu=False` ⇒ 今晚沒有 `cpu:` header、其餘有 ⇒ INCONCLUSIVE。
    `issue_body` 非 None ⇒ 有一張 open 的 perf-trend issue（#7）。
    """
    bindir = tmp / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH.format(python=sys.executable), encoding="utf-8")
    gh.chmod(0o755)
    ghdir = tmp / "fix" / "gh"
    ghdir.mkdir(parents=True, exist_ok=True)
    cache = tmp / "fix" / "bench-cache"
    runs = []
    for i in range(10):
        run_id = 5000 + i
        runs.append({"databaseId": run_id, "createdAt": f"2026-05-{28 - i:02d}T03:00:00Z",
                     "headSha": f"{i:040x}", "conclusion": "success"})
        ns = 39_000_000 if (regress and i < 3) else 35_000_000
        lines = [] if (i == 0 and not tonight_cpu) else [f"cpu: {_BENCH_CPU}"]
        lines += [f"{_BENCH}-8   \t      30\t  {ns} ns/op",
                  f"{_BENCH_CANARY}-8   \t    3000\t    360000 ns/op"]
        d = cache / f"run-{run_id}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "bench-baseline.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (d / ".download-complete").write_text("", encoding="utf-8")
    (ghdir / "runs.json").write_text(json.dumps(runs), encoding="utf-8")
    issues = [] if issue_body is None else [
        {"number": 7, "title": "perf-trend", "body": issue_body,
         "labels": [{"name": "perf-trend"}]}]
    (ghdir / "issues.json").write_text(json.dumps(issues), encoding="utf-8")
    return ["--trend-watch", "--cache-dir", str(cache), "--dry-run"]


def _bench_state_marker(state: list[list[str]]) -> str:
    """用工具自己的 renderer 產 state marker，格式不會與工具漂移。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_abh_for_dry_run_gate", TOOL_SCRIPTS["analyze_bench_history"])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # @dataclass looks its module up here
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return "legacy table\n" + mod._render_state_marker(state)


# ── 其餘 dx/lint 工具的 fixture ────────────────────────────────────────────
def _md_without_frontmatter(tmp: Path) -> str:
    # --base-dir 是專案根：工具只收 <base>/docs/**、<base>/rule-packs/** 與根目錄
    # 的 README/CHANGELOG。直接指到 docs/ 會什麼都不收（write_control 抓到過）。
    _write(tmp, "project/docs/guide.md", "# Guide\n\nSome body text.\n")
    return str(tmp / "fix" / "project")


def _related_docs(tmp: Path) -> str:
    for name, title in (("alpha.md", "Alpha"), ("beta.md", "Beta")):
        _write(tmp, f"related/{name}", (
            f"---\ntitle: {title}\ntags: [alerting, tenant]\n"
            f"audience: [platform]\nlang: zh\n---\n# {title}\n\nbody\n"))
    return str(tmp / "fix" / "related")


def _flat_conf_d_repo(tmp: Path) -> str:
    repo = _git_repo(tmp / "confrepo", {
        "conf.d/tenant-one.yaml": (
            "tenants:\n"
            "  tenant-one:\n"
            "    _metadata:\n"
            "      domain: finance\n"
            "      region: us-east\n"
            "      environment: prod\n"
            "    mysql_connections: '80'\n"),
    })
    return str(repo / "conf.d")


def _stale_rule_packs(tmp: Path) -> str:
    """rule-packs 副本、拿掉產物：寫入模式一定要**產生**東西，不能是寫回同一份
    bytes（那樣 sha 不變，拿掉守衛也不會紅——空的綠）。"""
    dest = _copy_into(tmp, RULE_PACKS, "rule-packs")
    for p in Path(dest).glob("ALERT-REFERENCE*.md"):
        p.unlink()
    return dest


def _playbook_project(tmp: Path) -> list[str]:
    # bump_playbook_versions 以 cwd 往上找 `.git` 當 repo root。
    proj = tmp / "proj"
    (proj / ".git").mkdir(parents=True, exist_ok=True)
    for rel in ("docs/internal/testing-playbook.md",
                "docs/internal/benchmark-playbook.md",
                "docs/internal/windows-mcp-playbook.md",
                "docs/internal/github-release-playbook.md"):
        (proj / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, proj / rel)
    return ["--to", "v99.0.0", "--dry-run"]


def _reword_repo(tmp: Path) -> list[str]:
    repo = _git_repo(tmp / "gitrepo", {"a.txt": "a\n"})
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "fixture: second commit")
    head = _git(repo, "rev-parse", "HEAD")
    mapping = _write(tmp, "reword.tsv", f"{head}\tchore: reworded by the gate\n")
    return [mapping, "--dry-run"]


def _ssot_pilot(tmp: Path) -> list[str]:
    pilot = _sb(tmp, "docs/pilot")
    pilot.mkdir(parents=True, exist_ok=True)
    (pilot / "guide.md").write_text(
        "---\ntitle: 指南\nlang: zh\n---\n# 指南\n\n> **Language / 語言：** "
        "**中文 (當前)** | [English](guide.en.md)\n", encoding="utf-8")
    (pilot / "guide.en.md").write_text(
        "---\ntitle: Guide\nlang: en\n---\n# Guide\n\n> **Language / 語言：** "
        "[中文](guide.md) | **English (Current)**\n", encoding="utf-8")
    # ⚠️ --execute 與 --dry-run 同給：這支的寫入要 --execute 才開，沒有它
    # dry-run 守衛根本擋不到任何東西。要驗的正是「兩個都給時 dry-run 贏」。
    return ["--directory", "docs/pilot", "--execute", "--dry-run"]


def _jsx_orchestrator(tmp: Path) -> list[str]:
    # ⚠️ scaffold_jsx_dep 目前找的是 `docs/interactive/tools/<parent>.jsx`，
    # 但 JSX 已搬到 `tools/portal/src/interactive/tools/`，所以在真的 repo 上
    # 每次呼叫都 rc=2（已寫回 issue #1454）。fixture 放在它**現在**去找的位置，
    # 量的是它的 dry-run 守衛，不是替那條過期路徑背書。
    orch = _sb(tmp, "docs/interactive/tools/probe-tool.jsx")
    orch.parent.mkdir(parents=True, exist_ok=True)
    orch.write_text(
        "---\ntitle: Probe\ndependencies: [\n  \"probe-tool/hooks/useExisting.js\"\n]\n---\n"
        "const useExisting = window.__useExisting;\n\nexport default function P() {}\n",
        encoding="utf-8")
    return ["--kind", "hook", "--name", "useDryRunProbe", "--parent", "probe-tool",
            "--dry-run"]


def _stale_tool_registry_outputs(tmp: Path) -> list[str]:
    """讓三個寫入守衛（flow map / hub / JSX frontmatter）各有東西可寫。"""
    _edit(_sb(tmp, "docs/assets/jsx-loader.html"),
          "'wizard': '../getting-started/wizard.jsx'",
          "'wizard': '../stale/wizard.jsx'")
    hub = _sb(tmp, "docs/interactive/index.html")
    text = hub.read_text(encoding="utf-8")
    new = re.sub(r'data-audience="[^"]*"(\s+href="[^"]*getting-started/wizard\.jsx")',
                 r'data-audience="stale"\1', text, count=1)
    assert new != text, "fixture 前提不成立：hub 裡找不到 wizard 卡片"
    hub.write_text(new, encoding="utf-8", newline="\n")
    # sync_frontmatter 讀 `tools/portal/src/<registry file>`（與
    # check_tool_registry_jsx_parity 同一個 JSX_ROOT）。
    jsx = _sb(tmp, "tools/portal/src/getting-started/wizard.jsx")
    jsx.parent.mkdir(parents=True, exist_ok=True)
    jsx.write_text("---\ntitle: Wizard\naudience: [nobody]\ntags: [stale]\n---\n"
                   "export default function W() {}\n", encoding="utf-8")
    return ["--sync-frontmatter", "--dry-run"]


def _fixable_doc_links(tmp: Path) -> list[str]:
    # ⚠️ fix_doc_links 的 DOCS_DIR 是 `lint/../../docs` = `scripts/docs`（搬進
    # lint/ 時少了一層 `..`），所以在真的 repo 上每次呼叫都 rc=2（已寫回
    # issue #1454）。fixture 放在它**現在**去找的位置。
    adr = _sb(tmp, "scripts/docs/adr")
    adr.mkdir(parents=True, exist_ok=True)
    (adr / "001-a.md").write_text("# A\n\nsee [B](adr/002-b.md)\n", encoding="utf-8")
    (adr / "002-b.md").write_text("# B\n", encoding="utf-8")
    return ["--dry-run"]


# ═══════════════════════════════════════════════════════════════════════════
# Stub server — reuses the --json gate's handler (Prometheus / Alertmanager
# personalities); only cutover_tenant / maintenance_scheduler /
# threshold_recommend touch it.
# ═══════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="session")
def stub_url():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _jsc._StubHandler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


# ═══════════════════════════════════════════════════════════════════════════
# Recipes — exactly one dry-run invocation per in-scope tool.
#
# Twelve argv shapes are lifted from test_json_stdout_contract.py's dry-run
# recipes (fixture paths swapped for tmp copies); six tools had no recipe
# there and get a minimal invocation here (baseline_discovery, da_assembler,
# generate_alertmanager_routes, generate_tenant_mapping_rules, init_project,
# migrate_rule).
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Recipe:
    tool: str
    build: Callable[[Path, str], list[str]]   # (tmp_path, stub_url) -> argv
    # Graceful exit codes for this invocation. 0 for almost every tool;
    # a tool whose dry-run legitimately ends in a detected-violation REPORT
    # (exit 1 = EXIT_VIOLATION) widens this — never 2 (caller error), which
    # would mean the recipe itself is broken.
    ok_exits: tuple[int, ...] = (0,)
    # ── 以下欄位為 dx/lint 擴建（#1454 D）而加，ops 的 recipe 全用預設值 ──
    # 同一支工具有多個寫入守衛、而單一次呼叫走不到全部時，一個守衛分支一個
    # variant（id 變成 `tool[variant]`）。見 docstring §2。
    variant: str = ""
    # 非 None ⇒ 把 `scripts/tools/` 與這些 repo 相對路徑複製成 `tmp/repo`，並
    # 從**那份副本**執行工具。給 REPO_ROOT 由 `__file__` 推導的工具用：直接跑
    # repo 裡那支，它的寫入會落在 repo 而不是快照看得到的 tmp tree。
    sandbox: tuple[str, ...] | None = None
    # 子行程的 cwd（相對 tmp_path）。
    cwd: str = "cwd"
    # 額外的環境變數；(tmp_path) -> dict。
    env: Callable[[Path], dict[str, str]] | None = None
    # 寫入模式（write_control 那次）可接受的 exit code。⚠️ 不預設沿用
    # ok_exits：dry-run 與寫入的 exit 契約可以不同（sync_tool_registry 的
    # dry-run 在有變更時 exit 1、寫入 exit 0）。
    write_ok_exits: tuple[int, ...] = (0,)
    # True ⇒ `test_write_mode_control` 會把 `--dry-run` 從 argv 拿掉再跑一次，
    # 並要求 tree **有**變。這是「recipe 真的走到會寫的那條路」的常設證明：
    # fixture 一旦被改成「沒有東西可寫」，dry-run 那條就會變成空的綠，而這條會紅。
    write_control: bool = False
    # 只在 POSIX 有意義的 recipe（假 `gh` 的 shebang shim；見 _fake_gh）。
    posix_only: bool = False

    @property
    def id(self) -> str:
        return f"{self.tool}[{self.variant}]" if self.variant else self.tool


R = Recipe
RECIPES: list[Recipe] = [
    # ── new minimal invocations (no --json-gate dry-run recipe existed) ────
    R("baseline_discovery",
      lambda t, s: ["--tenant", "tenant-one", "--dry-run"]),

    R("da_assembler",
      lambda t, s: ["--render-cr", _threshold_cr(t),
                    "--config-dir", _out(t, "rendered"), "--dry-run"]),

    R("generate_alertmanager_routes",
      lambda t, s: ["--config-dir", _copy_into(t, SEED_CONF_D, "conf.d"),
                    "--dry-run"]),

    R("generate_tenant_mapping_rules",
      lambda t, s: ["--config-dir", _copy_into(t, SEED_CONF_D, "conf.d"),
                    "--dry-run"]),

    R("init_project",
      lambda t, s: ["--non-interactive", "--tenants", "tenant-one,tenant-two",
                    "--dry-run", "-o", _out(t, "project")]),

    R("migrate_rule",
      lambda t, s: [_legacy_rules(t), "--dry-run",
                    "-o", _out(t, "migration")]),

    # ── argv shapes reused from the --json gate's dry-run recipes ──────────
    R("batch_diagnose",
      lambda t, s: ["--tenants", "tenant-one,tenant-two", "--dry-run", "--json"]),

    R("cutover_tenant",
      lambda t, s: ["--readiness-json", _jsc._readiness_json(t / "fix"),
                    "--tenant", "tenant-one",
                    "--prometheus", s, "--dry-run", "--json-output"]),

    # ok_exits: the CURRENT in-repo packs carry a clickhouse edge/central
    # metric mismatch, so the tool ends in its graceful detected-violation
    # report (exit 1 = EXIT_VIOLATION) — same result against the un-copied
    # repo dir, so it is the data, not this gate's fixture copy. The
    # stdout-report guard below keeps this from masking a crash.
    R("generate_rule_pack_split",
      lambda t, s: ["--rule-packs-dir", _copy_into(t, RULE_PACKS, "rule-packs"),
                    "--dry-run", "--json"],
      ok_exits=(0, 1)),

    R("grafana_import",
      lambda t, s: ["--dashboard", _copy_into(t, DASHBOARD_JSON, "dashboard.json"),
                    "--dry-run", "--json"]),

    R("maintenance_scheduler",
      lambda t, s: ["--config-dir", _copy_into(t, SEED_CONF_D, "conf.d"),
                    "--alertmanager", s, "--dry-run", "--json-output"]),

    R("migrate_to_operator",
      lambda t, s: ["--source-dir", _copy_into(t, K8S_MONITORING, "monitoring"),
                    "--config-dir", _copy_into(t, SEED_CONF_D, "conf.d"),
                    "--output-dir", _out(t, "crds"), "--dry-run", "--json"]),

    R("notification_tester",
      lambda t, s: ["--config-dir", _copy_into(t, SEED_CONF_D, "conf.d"),
                    "--dry-run", "--json"]),

    R("onboard_platform",
      lambda t, s: ["--alertmanager-config",
                    _copy_into(t, ALERTMANAGER_YML, "alertmanager.yml"),
                    "-o", _out(t, "onboard"), "--dry-run", "--json"]),

    R("operator_generate",
      lambda t, s: ["--rule-packs-dir", _copy_into(t, RULE_PACKS, "rule-packs"),
                    "--config-dir", _copy_into(t, SEED_CONF_D, "conf.d"),
                    "--output-dir", _out(t, "operator"), "--dry-run", "--json"]),

    R("policy_opa_bridge",
      lambda t, s: ["--config-dir", _copy_into(t, SEED_CONF_D, "conf.d"),
                    "--dry-run", "--json"]),

    R("state_reconcile",
      lambda t, s: ["--state-dir", _existing_dir(t, "state"),
                    "--manifest-path", _out(t, "manifest.json"),
                    "--dry-run", "--json"]),

    R("threshold_recommend",
      lambda t, s: ["--config-dir", _copy_into(t, SEED_CONF_D, "conf.d"),
                    "--prometheus", s, "--dry-run", "--json"]),

    # ── dx / lint（#1454 D）──────────────────────────────────────────────
    # 每一條都帶 write_control=True（除非註明）：同一份 argv 拿掉 --dry-run
    # 必須**真的**寫出東西，否則這條 dry-run 的綠是空的。
    R("add_frontmatter",
      lambda t, s: ["--base-dir", _md_without_frontmatter(t), "--dry-run"],
      write_control=True),

    # 五個 gh 寫入守衛各在一條分支上，單一次呼叫只走得到一條 ⇒ 五個 variant。
    R("analyze_bench_history", variant="open",
      build=lambda t, s: _bench_history(t, regress=True),
      env=_bench_env, write_control=True, posix_only=True),
    R("analyze_bench_history", variant="update",
      build=lambda t, s: _bench_history(t, regress=True, issue_body=""),
      env=_bench_env, write_control=True, posix_only=True),
    R("analyze_bench_history", variant="inconclusive",
      build=lambda t, s: _bench_history(t, regress=False, tonight_cpu=False,
                                        issue_body=""),
      env=_bench_env, write_control=True, posix_only=True),
    R("analyze_bench_history", variant="held-open",
      build=lambda t, s: _bench_history(
          t, regress=False,
          issue_body=_bench_state_marker([["BenchmarkRetiredProbe", "sustained"]])),
      env=_bench_env, write_control=True, posix_only=True),
    R("analyze_bench_history", variant="close",
      build=lambda t, s: _bench_history(t, regress=False, issue_body=""),
      env=_bench_env, write_control=True, posix_only=True),

    # 版號規則的三種寫法（pattern / pair_anchor / whole_file）各一個守衛；
    # --sync-counts 的第四個守衛在另一條分支、且不接受版號旗標 ⇒ 兩個 variant。
    # ok_exits=(1,)：沙箱只帶了少數目標檔，其餘規則回報 SKIP/DEAD ⇒ exit 1，
    # 寫入模式在同樣的 exit 1 下仍會寫（write_control 量的就是這個）。
    R("bump_docs", variant="versions",
      build=lambda t, s: ["--platform", "99.0.0", "--tools", "99.0.0", "--dry-run"],
      sandbox=("CLAUDE.md", "components/da-tools/app",
               "helm/federation-reconciler/values.yaml"),
      cwd="repo", ok_exits=(1,), write_ok_exits=(1,), write_control=True),
    R("bump_docs", variant="sync-counts",
      build=lambda t, s: (
          _edit(_sb(t, "CLAUDE.md"), " auto-run + ", "0 auto-run + ")
          or ["--sync-counts", "--dry-run"]),
      sandbox=("CLAUDE.md", ".pre-commit-config.yaml"),
      cwd="repo", ok_exits=(1,), write_ok_exits=(1,), write_control=True),

    R("bump_playbook_versions",
      lambda t, s: _playbook_project(t), cwd="proj", write_control=True),

    R("generate_alert_reference",
      lambda t, s: ["--output-dir", _stale_rule_packs(t), "--dry-run"],
      write_control=True),

    R("generate_platform_data",
      lambda t, s: ["--dry-run"],
      sandbox=("rule-packs", "k8s", "helm",
               "components/threshold-exporter/config/conf.d",
               "scripts/ops/check_image_refs_resolve.py"),
      cwd="repo", write_control=True),

    R("generate_tenant_metadata",
      lambda t, s: ["--config-dir", _copy_into(t, SEED_CONF_D, "conf.d"),
                    "--output", _out(t, "tenant-metadata.json"), "--dry-run"],
      write_control=True),

    # 下面兩支的寫入要寫入旗標（--update / --apply）才開，所以 recipe 把它與
    # --dry-run 同給——那正是「使用者以為自己在預覽」的情境。兩支原本都沒有
    # 守衛（--dry-run 從沒被讀過），現在是 dry-run 優先。
    R("inject_related_docs",
      lambda t, s: ["--docs-dir", _related_docs(t), "--update", "--dry-run"],
      write_control=True),

    R("migrate_conf_d",
      lambda t, s: ["--conf-d", _flat_conf_d_repo(t), "--apply", "--dry-run"],
      cwd="confrepo", env=lambda t: dict(_GIT_ENV), write_control=True),

    R("migrate_ssot_language",
      lambda t, s: _ssot_pilot(t),
      sandbox=(), cwd="repo", write_control=True),

    R("reword_chain",
      lambda t, s: _reword_repo(t),
      cwd="gitrepo", env=lambda t: dict(_GIT_ENV), write_control=True),

    R("scaffold_jsx_dep",
      lambda t, s: _jsx_orchestrator(t),
      sandbox=(), cwd="repo", write_control=True),

    R("scaffold_lint",
      lambda t, s: ["--name", "dry_run_probe", "--kind", "text",
                    "--description", "dry-run gate probe", "--dry-run"],
      sandbox=(".pre-commit-config.yaml",), cwd="repo", write_control=True),

    # ok_exits=(1,)：這支的 dry-run 在「有東西要改」時 exit 1（EXIT_VIOLATION）。
    R("sync_tool_registry",
      lambda t, s: _stale_tool_registry_outputs(t),
      sandbox=("docs/assets/tool-registry.yaml", "docs/assets/jsx-loader.html",
               "docs/interactive/index.html"),
      cwd="repo", ok_exits=(1,), write_control=True),

    # 沒有 write_control：verify_diff 的 `--dry-run` 是預設（列出選集）的顯式
    # 別名，本身沒有守衛；列表模式不寫任何東西。這條釘住的是「列表模式保持
    # 零寫入」，不是某個守衛。`--run`（真的跑 pytest）不在這條的範圍。
    R("verify_diff",
      lambda t, s: ["scripts/tools/dx/bump_docs.py", "--ack-external", "--dry-run"]),

    R("fix_doc_links",
      lambda t, s: _fixable_doc_links(t),
      sandbox=(), cwd="repo", write_control=True),
]


# ═══════════════════════════════════════════════════════════════════════════
# Meta-tests — keep the scope honest
# ═══════════════════════════════════════════════════════════════════════════
def test_fixture_paths_exist():
    missing = [str(p) for p in FIXTURE_PATHS if not p.exists()]
    assert not missing, f"gate fixtures missing from the repo: {missing}"


def test_recipe_table_covers_every_dry_run_tool():
    """A tool that grows --dry-run must gain a recipe, or this gate rots."""
    covered = {r.tool for r in RECIPES}
    uncovered = sorted(set(DRY_RUN_TOOLS) - covered)
    stale = sorted(covered - set(DRY_RUN_TOOLS))
    assert not uncovered, (
        f"{len(uncovered)} tool(s) declare --dry-run but have no recipe in "
        f"RECIPES: {uncovered}"
    )
    assert not stale, f"RECIPES names tool(s) out of scope: {stale}"


# 每個子目錄各有幾支宣告 --dry-run 的工具。分目錄釘而不是釘總數：總數不變
# 但一支從 ops/ 搬到 dx/ 也是範圍變動，應該被看見。
EXPECTED_DRY_RUN_TOOLS_PER_DIR = {"ops": 18, "dx": 15, "lint": 1}


def test_dry_run_tool_count_per_dir_is_pinned():
    per_dir: dict[str, int] = {}
    for name in DRY_RUN_TOOLS:
        d = TOOL_SCRIPTS[name].parent
        key = "." if d == TOOLS_ROOT else d.name
        per_dir[key] = per_dir.get(key, 0) + 1
    assert per_dir == EXPECTED_DRY_RUN_TOOLS_PER_DIR, (
        f"argparse-declared --dry-run tools per dir moved: {per_dir} != "
        f"{EXPECTED_DRY_RUN_TOOLS_PER_DIR}. A new tool needs a recipe "
        "(test_recipe_table_covers_every_dry_run_tool says which); then update "
        "this dict deliberately."
    )


# 在 repo 裡、卻**刻意**不在這個閘門範圍內的 --dry-run 宣告。鍵是 repo 相對路徑。
OUT_OF_SCOPE_DECLARERS: dict[str, str] = {
    "tests/shared/test_dry_run_no_write.py": (
        "this file — its docstring and comments quote `add_argument(\"--dry-run\")`"
        " as the predicate being described; it is not a CLI tool"),
}
_REPO_SCAN_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv",
                                  "venv", ".tox", "site", "dist", "build"})


def test_no_dry_run_declarer_outside_the_gate():
    """⛔ 分母是**整個 repo**，不是這個閘門自己走的那幾個目錄（#1454 D）。

    這個閘門曾經只走 `scripts/tools/ops/`，而它的範圍測試也只數 `dx/`——於是
    `lint/fix_doc_links.py` 連「被數成洞」都沒有。量範圍的那一把尺，本身不能
    是被量的那個目錄清單的函數。這條把整個 repo 的 `*.py` 都掃一遍，任何一個
    宣告了 `--dry-run` 卻不在 RECIPES 裡、也沒有列在 OUT_OF_SCOPE_DECLARERS
    （附理由）的檔案都會紅。
    """
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _REPO_SCAN_SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = Path(dirpath) / fn
            try:
                src = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if DRY_RUN_FLAG_RE.search(src):
                found.add(p.relative_to(REPO_ROOT).as_posix())
    covered = {TOOL_SCRIPTS[r.tool].relative_to(REPO_ROOT).as_posix()
               for r in RECIPES}
    uncovered = sorted(found - covered - set(OUT_OF_SCOPE_DECLARERS))
    stale = sorted(set(OUT_OF_SCOPE_DECLARERS) - found)
    assert not uncovered, (
        f"{len(uncovered)} file(s) declare --dry-run and are NOT covered by this "
        f"gate: {uncovered}. Give each a recipe, or list it in "
        "OUT_OF_SCOPE_DECLARERS with the reason it is not a CLI tool."
    )
    assert not stale, f"OUT_OF_SCOPE_DECLARERS entries no longer declare: {stale}"
    # 反向護欄：掃描本身壞掉（例如 skip 了整棵 scripts/）會讓上面兩條空轉成綠。
    assert covered <= found, (
        f"repo scan missed recipe'd tools — the walk is broken: "
        f"{sorted(covered - found)}")


def test_tool_names_are_unique_across_dirs():
    stems = [f.stem for f in _tool_files()]
    dupes = sorted({s for s in stems if stems.count(s) > 1})
    assert not dupes, (
        f"tool name(s) exist in more than one scripts/tools/ subdir: {dupes} — "
        "TOOL_SCRIPTS is keyed by name, so one would silently shadow the other")


# 允許多於一條 recipe 的工具，以及理由（見 docstring §2）。
MULTI_VARIANT_TOOLS: dict[str, str] = {
    "analyze_bench_history": (
        "five gh-write guards (open / update / inconclusive / held-open / close), "
        "each on a different trend-watch branch"),
    "bump_docs": (
        "the version-rule guards and the --sync-counts guard are on branches "
        "that reject each other's flags"),
}


def test_one_recipe_per_tool():
    """Runtime discipline: one subprocess per tool (see docstring §2) — except
    where one invocation cannot reach every write guard, and then one per
    guarded branch, each named."""
    ids = [r.id for r in RECIPES]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicated recipe id(s): {dupes}"
    tools = [r.tool for r in RECIPES]
    multi = sorted({t for t in tools if tools.count(t) > 1})
    assert multi == sorted(MULTI_VARIANT_TOOLS), (
        f"tools with >1 recipe {multi} != MULTI_VARIANT_TOOLS "
        f"{sorted(MULTI_VARIANT_TOOLS)} — a second recipe needs a stated reason")
    unnamed = sorted({r.tool for r in RECIPES
                      if r.tool in MULTI_VARIANT_TOOLS and not r.variant})
    assert not unnamed, f"multi-recipe tool(s) with an unnamed recipe: {unnamed}"


def test_mention_only_allowlist_is_exact():
    """Files mentioning --dry-run without declaring it need an explicit entry.

    Catches BOTH drifts: a new passthrough tool advertising the flag in help
    text (must be triaged into a recipe or this allowlist), and a listed tool
    that starts genuinely declaring the flag (then it belongs in RECIPES and
    its allowlist entry is stale).
    """
    mention_only = {
        name for name, src in _tool_sources().items()
        if DRY_RUN_MENTION_RE.search(src) and not DRY_RUN_FLAG_RE.search(src)
    }
    assert mention_only == set(MENTION_ONLY_ALLOWLIST), (
        f"mention-only set drifted.\n"
        f"  unlisted (triage: recipe or allowlist+reason): "
        f"{sorted(mention_only - set(MENTION_ONLY_ALLOWLIST))}\n"
        f"  stale allowlist entries: "
        f"{sorted(set(MENTION_ONLY_ALLOWLIST) - mention_only)}"
    )


def test_known_writers_are_in_scope():
    """No stale exemptions: every KNOWN_DRY_RUN_WRITERS key must be a recipe."""
    tools = {r.tool for r in RECIPES}
    stale = sorted(set(KNOWN_DRY_RUN_WRITERS) - tools)
    assert not stale, f"KNOWN_DRY_RUN_WRITERS lists non-recipe tool(s): {stale}"


# ═══════════════════════════════════════════════════════════════════════════
# The gate
# ═══════════════════════════════════════════════════════════════════════════
def _params():
    out = []
    for r in RECIPES:
        marks = []
        if r.tool in KNOWN_DRY_RUN_WRITERS:
            marks.append(pytest.mark.xfail(
                strict=True,
                reason=f"known dry-run writer: {KNOWN_DRY_RUN_WRITERS[r.tool]}",
            ))
        out.append(pytest.param(r, id=r.id, marks=marks))
    return out


def _prepare(recipe: Recipe, tmp_path: Path, stub_url: str) -> list[str]:
    if recipe.posix_only and os.name == "nt":
        pytest.skip("fake `gh` is a shebang shim; Windows resolves gh.exe and "
                    "would run the REAL gh (see _FAKE_GH)")
    if recipe.sandbox is not None:
        _make_sandbox(tmp_path, recipe.sandbox)
    argv = recipe.build(tmp_path, stub_url)
    (tmp_path / recipe.cwd).mkdir(parents=True, exist_ok=True)
    return argv


def _run(recipe: Recipe, tmp_path: Path, argv: list[str]):
    """Run the tool once; return (proc, added, removed, changed)."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"      # conftest sets it too; explicit here
    env.pop("PROMETHEUS_URL", None)        # never let a stray env var redirect us
    env.pop("ALERTMANAGER_URL", None)
    # 沙箱裡跑的是 scripts/tools 的副本：不關 bytecode 快取，import 就會在
    # tmp tree 長出 __pycache__/，被快照算成工具的寫入。
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # 在 GitHub Actions 裡跑時，這個變數指向**真的** job summary；工具（如
    # analyze_bench_history）會往裡 append。那是 tmp tree 外、而且是別人的檔案。
    env.pop("GITHUB_STEP_SUMMARY", None)
    if recipe.env is not None:
        env.update(recipe.env(tmp_path))

    before = _tree_snapshot(tmp_path)

    if recipe.sandbox is not None:
        script = (tmp_path / "repo" /
                  TOOL_SCRIPTS[recipe.tool].relative_to(REPO_ROOT))
    else:
        script = TOOL_SCRIPTS[recipe.tool]
    proc = subprocess.run(
        [sys.executable, str(script), *argv],
        capture_output=True, timeout=TIMEOUT_S, cwd=str(tmp_path / recipe.cwd),
        env=env,
    )

    after = _tree_snapshot(tmp_path)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after)
                     if before[k] != after[k])
    return proc, added, removed, changed


@pytest.mark.parametrize("recipe", _params())
def test_dry_run_leaves_filesystem_untouched(recipe: Recipe, tmp_path: Path,
                                             stub_url: str):
    """--dry-run ⇒ the tmp tree is byte-identical before and after the run."""
    argv = _prepare(recipe, tmp_path, stub_url)
    proc, added, removed, changed = _run(recipe, tmp_path, argv)

    stdout = proc.stdout.decode("utf-8", "replace")
    stderr = proc.stderr.decode("utf-8", "replace")

    def fail(why: str) -> str:
        return (
            f"\n--dry-run zero-side-effect contract VIOLATED\n"
            f"  tool  : {recipe.id} ({TOOL_SCRIPTS[recipe.tool].relative_to(REPO_ROOT).as_posix()})\n"
            f"  argv  : {argv}\n"
            f"  reason: {why}\n"
            f"  exit  : {proc.returncode}\n"
            f"  stdout[:200]: {stdout[:200]!r}\n"
            f"  stderr[:300]: {stderr[:300]!r}\n"
            f"  fix   : the dry-run branch must return BEFORE any "
            f"mkdir/write/unlink — preview means preview.\n"
        )

    # Guard against a vacuous pass: a crash touches nothing and proves nothing.
    assert proc.returncode in recipe.ok_exits, fail(
        f"recipe exited {proc.returncode}, expected one of {recipe.ok_exits} — "
        f"the dry-run path was never (fully) walked, so an unchanged tree "
        f"would be a vacuous green"
    )
    if proc.returncode != 0:
        # A graceful non-zero MUST come with its report; an unhandled
        # traceback also exits 1 but leaves stdout empty.
        assert stdout.strip(), fail(
            "non-zero exit with EMPTY stdout — that is a crash, not a "
            "graceful detected-violation report"
        )
        if "--json" in argv:
            # Mid-report crash leaves partial (unparseable) output that a
            # bare non-empty check would wave through (W6a blind-review F2).
            try:
                json.loads(stdout)
            except ValueError:
                raise AssertionError(fail(
                    "non-zero exit with UNPARSEABLE --json stdout — "
                    "partial report = crash, not a graceful violation report"
                )) from None

    if added or removed or changed:
        raise AssertionError(fail(
            "filesystem changed during --dry-run:\n"
            + json.dumps({"added": added, "removed": removed,
                          "changed": changed}, indent=2)
        ))


def _control_params():
    return [pytest.param(r, id=r.id) for r in RECIPES if r.write_control]


@pytest.mark.parametrize("recipe", _control_params())
def test_write_mode_control(recipe: Recipe, tmp_path: Path, stub_url: str):
    """同一份 fixture、拿掉 `--dry-run` ⇒ tree **必須**改變（#1454 D）。

    dry-run 那條測的是「守衛有擋」；它的前提是「沒有守衛就會寫」。前提不成立
    時——fixture 早已是最新、工具找不到輸入、寫入被別的條件擋掉——dry-run 那條
    照樣綠，但那是空的：拿掉守衛也不會紅。這條把那個前提變成常設斷言，而不是
    落地那一次手動量過就算數。
    """
    argv = _prepare(recipe, tmp_path, stub_url)
    assert "--dry-run" in argv, f"{recipe.id}: recipe argv lacks --dry-run"
    write_argv = [a for a in argv if a != "--dry-run"]
    proc, added, removed, changed = _run(recipe, tmp_path, write_argv)
    stderr = proc.stderr.decode("utf-8", "replace")
    # 先驗「寫入模式正常結束」：寫了一半就 crash 的 run 也會讓 tree 改變，
    # 只看下面那條會把它當成「前提成立」（CodeRabbit 在 PR #1959 指出）。
    assert "Traceback (most recent call last)" not in stderr, (
        f"{recipe.id}: write-mode run crashed. stderr[-400:]: {stderr[-400:]!r}")
    assert proc.returncode in recipe.write_ok_exits, (
        f"{recipe.id}: write-mode exit {proc.returncode}, expected one of "
        f"{recipe.write_ok_exits}. stderr[-400:]: {stderr[-400:]!r}")
    assert added or removed or changed, (
        f"{recipe.id}: the same fixture WITHOUT --dry-run wrote nothing "
        f"(exit {proc.returncode}) — the dry-run recipe for it is a vacuous "
        f"green. stderr[-400:]: {stderr[-400:]!r}"
    )
