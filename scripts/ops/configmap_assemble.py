"""`make configmap-assemble` 的閘門**兼產生器**：選檔、引用、產出都在這裡。

`configmap-assemble` 是客戶的部署步驟（`docs/integration/gitops-deployment.md`
教 `make configmap-assemble` → `kubectl apply`）。這支原本叫
`configmap_assemble_precheck.py`，只回答「擋不擋」；recipe 另外用兩個
`$(shell …)` 各自列舉一次要進 ConfigMap 的檔。**同一個問題三份列舉**，於是
三種失效各自成立（#1792 / #1796）。現在收攏成一份列舉、一個 producer。

⛔ 三個刻意的邊界（沿用，未變）：

1. **不重寫規則**——`validate_config` 的 `tenant_uniqueness`（#1577）已經在
   回答重複租戶那個問題，這支只消費它的裁決。量測與三態分類住在
   `_lib_tenant_uniqueness`，與 `assemble_config_dir`（另一個 producer，
   #1794）共用；**措辭不共用**，因為兩者拒絕的理由不同。
2. **不採用它的完整裁決**——那會讓客戶樹只要有任何一項無關違規就從「能部署」
   變成「不能部署」，是與本軸無關的迴歸。
3. **「量不到」不是「量了沒事」**——JSON 解不出來、那一項不在輸出裡、或它回的
   **不是**肯定的 `pass`（`tenant_uniqueness` 的 `warn` 逐字說「this is a limit
   on what was checked, not a clean result」），一律 `EXIT_CALLER_ERROR`。

⛔ 裁決的對象是**產物、不是那棵樹**：ConfigMap 的 key 平面表達不出子目錄，所以
只有頂層那組檔進得去。拿整棵樹去問，會用一個永遠不會出貨的重複宣告（例如
`examples/` 底下那份）擋掉一個其實可部署的產物。子目錄裡的租戶不會出貨這件事由
`warn_nested` 說出來——那是同一份誠實的另一半。

三個新軸：

- **#1792 大小寫**：列舉改用 exporter 自己的述詞 `has_yaml_extension`
  （大小寫不敏感），所以 `DB-A.YAML` 這種載體現在**會**進 ConfigMap。原本
  shell glob 的 `*.yaml` / `*.yml` 是 shell 用大小寫敏感的 fnmatch 比對
  readdir 的結果，與檔案系統折不折大小寫無關 ⇒ 那是**固定的丟棄**，每個平台
  都漏。既然兩份列舉合一，原本回報差集的那則 WARN 沒有對象了，一併刪除。
- **#1796 shell 解析**：`--from-file=` 引數改由 `subprocess.run([...])` 以
  argv list 交給 `kubectl`（`shell=False`），中間沒有任何一層會再解析一次
  檔名。`db b.yaml` 不再讓 `basename` 收到兩個引數而掉副檔名，
  `db-a (copy).yaml` 也不再把整條 recipe 炸成無法診斷的語法錯誤。⛔ 但
  「穿得過 shell」不等於「能當 key」——見下面 `configmap_key_problem`。
- **#1797 範例樹**：`CONFDIR` 的預設值指向本 repo 自帶的開發範例樹
  （`db-a` / `db-b`）。照文件的 CI 片段跑會把示範租戶 apply 上生產，所以
  這支對那棵樹**硬擋**，要明示 `ALLOW_SAMPLE_CONFDIR=1` 才放行。
  ⚠️ **這道擋是 per-checkout 不是 per-repo**，這是**揭露不是疏漏**：
  `SAMPLE_CONFIG_DIR` 由 `__file__` 推導，所以它守的是「跑這一份 script 的
  這一棵樹」。另一個 checkout／worktree 的同一份範例樹要**明示打出那條路徑**
  才碰得到，而那已經不是「照預設值跑」這個缺陷類別了。改用 git 收斂 repo
  identity 的代價更壞：客戶樹上未必有 `git`，而缺 `git` 時那道判定會**靜默
  放行**——一個只在測得到的地方才成立的守衛。

⛔ 不自己重寫 kubectl 的 YAML 產生器：`kubectl` 本來就是這條 recipe 的既有
依賴，第二份產生器只會與它漂移。

⛔ **這支擋的是「產物不合法」，一律在寫出產物之前；它不是內容政策。**
檔名能不能當 key 由 `configmap_key_problem`（轉寫自 `IsConfigMapKey`）在
呼叫 kubectl 之前回答——那一面**必須**先問，因為非法檔名的代價是整個租戶
無聲消失。

⛔ 其餘幾面**不再逐層轉寫 kubectl 的 parser**，改成產出之後回頭讀自己的
產物：`measure_artifact` 把 manifest 的 key 集合與長度，和我方 `carriers`
的意圖對帳，總位元組也在**已 parse 的產物**上量。round 3 的量測是這樣長
的——`--from-file` 的值先過 pflag `readAsCSV` 才輪到 `ParseFileSource`，
所以只轉寫最內層的守衛對路徑裡的 `,` 與 `"` 完全看不見；再補一層就再冒一
層。對帳問的是「產物是不是這棵樹」，kubectl 換 parser 也不影響。

⚠️ **量得到與量不到的界線**：`--config-dir` 底下**讀不到**的 config-named
entry（斷鏈 symlink、同名目錄）不會讓這支紅，但一定**逐個具名**在 stderr
——沉默地丟掉它，等於這支自己的 docstring 在講的那件事。

⚠️ **make 那一層還有一段不屬於本檔射程的展開**：`make configmap-assemble
CONFDIR=...` 的值由 make 自己遞迴展開，所以值裡的 `$(shell …)` 會在
export 之前就執行。那是 GNU make 對命令列變數的定義，makefile 內無法關掉；
recipe 這一側能做的（不讓 **shell** 再解析一次）已經做了——recipe 讀的是
`"$$CONFDIR"` 這個 shell 變數，不是被貼進命令文字的值。
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

_THIS_DIR = Path(__file__).resolve().parent
_TOOLS = _THIS_DIR.parent / "tools"
sys.path.insert(0, str(_TOOLS))
import _lib_io  # noqa: E402
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK,
    EXIT_VIOLATION,
    EXIT_CALLER_ERROR,
)
from _lib_confd import (  # noqa: E402
    WARN_LIMIT as _WARN_LIMIT,
    has_yaml_extension,
    is_hidden_name,
    printable_name,
    unusable_config_entries,
    unusable_reason,
    warn_nested,
)
import _lib_tenant_uniqueness as tu  # noqa: E402

#: ⛔ Derived, never spelled out: this file is `<repo>/scripts/ops/<name>.py`.
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The development sample tree the `CONFDIR` default points at (#1797).
#: `db-a` / `db-b` are reference templates that ship in neither the chart nor
#: the image, so assembling them is never what a customer meant.
SAMPLE_CONFIG_DIR = _REPO_ROOT / "components" / "threshold-exporter" / "config" / "conf.d"

#: The escape hatch for the one caller who does mean it (docs, demos, this
#: repo's own tests).
ALLOW_SAMPLE_ENV = "ALLOW_SAMPLE_CONFDIR"

DEFAULT_OUTPUT = _REPO_ROOT / ".build" / "threshold-config.yaml"
DEFAULT_NAMESPACE = "monitoring"
DEFAULT_NAME = "threshold-config"

#: Bounded because `make configmap-assemble` is customer-run: an unbounded
#: wait there is a silent wedge with no output at all.
_KUBECTL_TIMEOUT_S = 120

# ── ConfigMap key legality (#1796) ───────────────────────────────────
#
# ⛔ NOT from memory. Transcribed from the authority, k8s.io/apimachinery
# `pkg/util/validation/validation.go`:
#
#   const configMapKeyFmt = `[-._a-zA-Z0-9]+`
#   var configMapKeyRegexp = regexp.MustCompile("^" + configMapKeyFmt + "$")
#   func IsConfigMapKey(value string) []string {
#       if len(value) > DNS1123SubdomainMaxLength { ... }   // 253, BYTES
#       if !configMapKeyRegexp.MatchString(value) { ... }
#       errs = append(errs, hasChDirPrefix(value)...)       // "." ".." "..*"
#   }
#
# `hasChDirPrefix` is the shared helper `IsValidPathSegmentName` also uses; it
# rejects `.`, `..` and any name starting with `..`. Go's `len()` counts
# BYTES, hence the encode below rather than `len(name)`.
#
# ⛔ `\Z`, not `$`: Python's `$` also matches just BEFORE a trailing newline,
# so `db-a.yaml\n` would have been accepted as a key. Go's `regexp` `$`
# (without `(?m)`) means end-of-text, which is `\Z` here — transcribing `$`
# to `$` copied the character and lost the meaning.
_CONFIGMAP_KEY_RE = re.compile(r"\A[-._a-zA-Z0-9]+\Z")
_MAX_KEY_BYTES = 253  # DNS1123SubdomainMaxLength

#: What a ConfigMap's `data` may total, transcribed from k8s core validation
#: (`ValidateConfigMap`: `totalSize > core.MaxSecretSize` -> "may not exceed
#: 1048576 bytes"). ⛔ Summed over the VALUES, not over the manifest.
_MAX_CONFIGMAP_BYTES = 1024 * 1024


def _name_bytes(name: str) -> bytes:
    """The bytes the API server will count for `name`, never raising."""
    try:
        return os.fsencode(name)
    except UnicodeEncodeError:
        # A synthesised name under a non-UTF-8 locale. UTF-8 is what the
        # API server counts, and `surrogatepass` keeps a lone surrogate
        # (what `fsdecode` hands back for an undecodable byte) countable.
        return name.encode("utf-8", "surrogatepass")


def configmap_key_problem(name: str) -> str | None:
    """Why `name` cannot be a ConfigMap key, or `None` if it can.

    ⛔ This is a legality question, not a taste question. A file called
    `db b.yaml` or `db-a (copy).yaml` can NEVER become a key, whatever the
    producer does — so the only honest answers are "rename it" or "leave it
    out on purpose". Dropping it silently is how a tenant disappears with a
    green light (#1603's shape), and handing it to `kubectl` produces an
    error that never names the file.

    ⛔ `os.fsencode`, not `name.encode("utf-8")`. A file name is BYTES on
    POSIX; Python hands it back with the undecodable ones smuggled in as
    lone surrogates, and `.encode("utf-8")` then raises `UnicodeEncodeError`
    — a codec traceback in place of the by-name refusal this function exists
    to produce. `os.fsencode` reverses the same escape, so the count is the
    byte count the API server will apply, for every name the filesystem can
    hand us.

    ⛔ …and it raises the same `UnicodeEncodeError` on a name that did NOT
    come from the filesystem, once the locale's filesystem encoding is not
    UTF-8: `os.fsencode` then encodes with `ascii`. No production path
    reaches that (names come from `iterdir`, so they carry the escape
    `fsencode` reverses), but a function whose whole job is "refuse by name
    instead of raising" may not have an input class it raises on.
    """
    if len(_name_bytes(name)) > _MAX_KEY_BYTES:
        return f"longer than {_MAX_KEY_BYTES} bytes"
    if not _CONFIGMAP_KEY_RE.match(name):
        bad = sorted({c for c in name if not _CONFIGMAP_KEY_RE.match(c)})
        shown = " ".join(repr(c) for c in bad) or "(empty name)"
        return f"contains characters a key may not hold: {shown}"
    if name in (".", ".."):
        return f"must not be {name!r}"
    if name.startswith(".."):
        return "must not start with '..'"
    return None


# ── what the ARTIFACT has to say back (#1796, second half) ───────────
#
# ⛔ REMOVED here: `from_file_source_problem`, which transcribed kubectl's
# `ParseFileSource` and counted `=`. It was one layer short — `--from-file`
# is a pflag `StringSliceVar`, so `readAsCSV` splits the value first and a
# path holding `,` or `"` became fabricated sources with the transcription
# silent. **The cost of removing it**: a `--config-dir` holding `=` is no
# longer named by US; kubectl still refuses and the run still fails, but its
# message blames "key names or file paths" and names neither.
# `measure_artifact` below asks the question no parser change can move.
#
# ⛔ `kubectl apply -f` — the client-side apply the deployment doc teaches —
# stores the ENTIRE object in the `last-applied-configuration` ANNOTATION,
# and `ValidateObjectMeta` (run first, before the `data` rule) caps an
# object's annotations at `TotalAnnotationSizeLimitB`, a QUARTER of the data
# ceiling. Under it a 400 KB tree passes every check here and dies at apply
# on `metadata.annotations: Too long`, naming no file.
_ANNOTATION_TOTAL_LIMIT = 256 * 1024


def _carriers(config_dir: Path) -> tuple[list[Path], list[Path]]:
    """The one enumeration: what the exporter reads at THIS level, plus the
    config-NAMED entries at this level that cannot be read at all.

    ⛔ Flat (`iterdir`, no recursion) because the ConfigMap key plane cannot
    express a subdirectory — `warn_nested` says that out loud.

    ⛔ `has_yaml_extension` is case-INSENSITIVE, matching the exporter's own
    scanner; that is #1792. `is_hidden_name` is not decoration either: the
    exporter skips `.`-prefixed entries (`config_hierarchy.go`), and counting
    them made the empty-dir guard below pass for a `--config-dir` pointed at
    a repo root — the exact mis-pointing that guard exists for.

    ⛔ The second list exists because `is_file()` is a SILENT filter: a
    dangling symlink and a directory called `db-x.yaml/` both carry a config
    name and neither is a file, so both used to leave the run with rc 0 and
    one tenant fewer than the tree declares — the exact "a tenant disappears
    with a green light" this module's own docstring names. The classification
    is `_lib_confd.unusable_config_entries` / `unusable_reason`, the pair the
    operator-plane readers already phrase this finding with; a second wording
    here would put two answers to "what happened to db-x.yaml" in front of
    the same person.
    """
    entries = sorted(config_dir.iterdir())
    carriers = [
        p for p in entries
        if p.is_file() and not is_hidden_name(p.name)
        and has_yaml_extension(p.name)
    ]
    return carriers, unusable_config_entries(entries)


def measure_artifact(manifest: str, carriers: list[Path]) -> tuple[list[str], dict[str, int]]:
    """Reconcile the manifest kubectl produced against the tree we asked for.

    Returns `(disagreements, bytes_per_key)`. A disagreement is a sentence
    naming one key; `bytes_per_key` is what the API server will SUM, read off
    the artifact rather than predicted from the tree.

    ⛔ Two independent sources, neither a model of kubectl's argument parser:
    what we meant (`carriers`, from `iterdir`) and what kubectl emitted (this
    YAML). Every way the argument can be mangled on the way in — the pflag
    CSV split on `,` and `"`, `ParseFileSource`'s split on `=`, whatever the
    next release adds — ends in the same place: a missing key, a key nobody
    asked for, or a value whose length is not the file's.

    ⚠️ It does NOT compare the value BYTES, only their count: that would
    assert a YAML round-trip through kubectl's emitter and this loader is the
    identity, which has never been measured here against a real kubectl.
    """
    try:
        doc = _lib_io.safe_load(manifest)
    except yaml.YAMLError as exc:
        return [f"kubectl's output is not parseable YAML: {exc}"], {}
    if not isinstance(doc, dict):
        return ["kubectl's output is not a YAML mapping"], {}

    got: dict[str, int | None] = {}
    for key, value in (doc.get("data") or {}).items():
        got[str(key)] = (len(value.encode("utf-8"))
                         if isinstance(value, str) else None)
    for key, value in (doc.get("binaryData") or {}).items():
        try:
            got[str(key)] = len(base64.b64decode(value, validate=True))
        except (ValueError, TypeError):
            got[str(key)] = None

    want = {p.name: p.stat().st_size for p in carriers}
    problems = []
    for name in sorted(set(want) - set(got)):
        problems.append(f"{name!r} is in {want[name]} bytes on disk but is "
                        f"ABSENT from the manifest — that tenant has no "
                        f"alerting")
    for name in sorted(set(got) - set(want)):
        problems.append(f"{name!r} is in the manifest but is not a carrier "
                        f"in --config-dir")
    for name in sorted(set(got) & set(want)):
        if got[name] is None:
            problems.append(f"{name!r} came back in a form this check cannot "
                            f"measure, so it was not verified")
        elif got[name] != want[name]:
            problems.append(f"{name!r} carries {got[name]} bytes in the "
                            f"manifest but holds {want[name]} on disk")
    return problems, {k: v for k, v in got.items() if v is not None}


def _forward(verdict: tu.Verdict) -> None:
    """Print the check's own details and hint. It knows more than we do."""
    for line in verdict.details:
        print(f"  {line}", file=sys.stderr)
    if verdict.action:
        print(f"  -> {verdict.action}", file=sys.stderr)


def _write_atomically(output: Path, text: str) -> None:
    """Materialise `output` only once its full content exists.

    A truncated-then-failed write leaves a half artifact that `kubectl apply`
    would happily consume; a failure before this point must leave whatever is
    on disk untouched, stale or absent.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(output.parent),
                                    prefix=output.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, output)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Gate and build the threshold-config ConfigMap "
                    "(#1603 / #1792 / #1796 / #1797).")
    ap.add_argument("--config-dir", required=True,
                    help="conf.d directory the ConfigMap is assembled from")
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT),
                    help=f"where to write the manifest (default: {DEFAULT_OUTPUT})")
    ap.add_argument("--namespace", default=DEFAULT_NAMESPACE,
                    help=f"ConfigMap namespace (default: {DEFAULT_NAMESPACE})")
    ap.add_argument("--name", default=DEFAULT_NAME,
                    help=f"ConfigMap name (default: {DEFAULT_NAME})")
    args = ap.parse_args(argv)

    config_dir = Path(args.config_dir)
    if not config_dir.is_dir():
        print(f"ERROR: config-dir not found: {config_dir}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    # #1797. `resolve()` on BOTH sides so a symlink, a `./` prefix or a `..`
    # detour cannot walk around it.
    #
    # ⛔ SUBTREE, not equality. The question is "is this the repo's sample
    # material", and `examples/` — same tree, same demo tenants, one level
    # down — answered it `no` under equality and assembled seven reference
    # files with rc 0. Path equality is a spelling of the question, not the
    # question. `in .parents` is exact containment on the RESOLVED path, so a
    # sibling tree outside the repo that merely ends in the same components
    # (`…/threshold-exporter/config/conf.d`) is untouched.
    resolved = config_dir.resolve()
    sample = SAMPLE_CONFIG_DIR.resolve()
    if ((resolved == sample or sample in resolved.parents)
            and os.environ.get(ALLOW_SAMPLE_ENV) != "1"):
        print(
            f"ERROR: refusing to assemble the repo's DEVELOPMENT SAMPLE tree: "
            f"{resolved}\n"
            f"       (at or below {SAMPLE_CONFIG_DIR})\n"
            f"       That tree is this repo's demonstration material and the "
            f"built-in `CONFDIR` default. Its tenants are reference "
            f"templates — they ship in neither the chart nor the image, and "
            f"they are almost certainly not yours. (Naming them here would "
            f"be wrong for the subdirectories: `examples/` declares another "
            f"set entirely.) `kubectl apply` of this artifact REPLACES the "
            f"live "
            f"`{args.name}` with the samples.\n"
            f"       -> point the target at your own tree: "
            f"`make configmap-assemble CONFDIR=/path/to/your/conf.d`\n"
            f"       -> or, if assembling the samples really is the intent "
            f"(docs, demo, this repo's own tests): "
            f"`{ALLOW_SAMPLE_ENV}=1 make configmap-assemble`",
            file=sys.stderr,
        )
        return EXIT_VIOLATION

    # This step is FLAT — a ConfigMap key plane cannot express a subdirectory.
    # `_lib_confd`'s contract is that a flat reader says so out loud.
    warn_nested(config_dir, tool="configmap_assemble")

    carriers, unusable = _carriers(config_dir)
    # ⚠️ Non-blocking, and named one by one. Blocking would be the wrong
    # trade (a tree that deploys today would stop deploying over an entry
    # nothing ever read), but staying silent is the failure this file is
    # named after: the count in the success line would simply be one lower
    # than the tree, with nothing saying which tenant went missing.
    # ⚠️ Bounded, and at the SAME 5 as `_lib_confd`'s nested warning, which
    # the run above just printed to this same stderr: one plane may not
    # carry two truncation policies, or the operator has to know which
    # reader wrote which line to know whether a list is complete.
    for p in unusable[:_WARN_LIMIT]:
        print(f"WARN: {printable_name(p.name)!r} in {config_dir} "
              f"{unusable_reason(p)} — it carries a config name but NOTHING "
              f"was read from it, so it is absent from the ConfigMap and its "
              f"tenants have no alerting.", file=sys.stderr)
    if len(unusable) > _WARN_LIMIT:
        print(f"WARN: (+{len(unusable) - _WARN_LIMIT} more unreadable "
              f"config-named entries in {config_dir}; this list is capped "
              f"at {_WARN_LIMIT})", file=sys.stderr)

    if not carriers:
        print(
            f"ERROR: {config_dir} contains no config carrier — the ConfigMap "
            f"would be built with zero --from-file args. That is a "
            f"mis-pointed --config-dir far more often than an intentional "
            f"deploy.\n"
            f"       ⚠️ NOT a tenant-count policy: a dir holding only "
            f"`_defaults.yaml` is allowed, because the ConfigMap then "
            f"faithfully projects a platform that has no tenants yet. "
            f"Whether a tree SHOULD have tenants is validate_config's plane.",
            file=sys.stderr,
        )
        return EXIT_VIOLATION

    # #1796. Name them one by one: the operator has to know WHICH file, and
    # `kubectl`'s own refusal would not say.
    illegal = [(p.name, why) for p in carriers
               if (why := configmap_key_problem(p.name)) is not None]
    if illegal:
        print(
            f"ERROR: {len(illegal)} file name(s) in {config_dir} cannot be a "
            f"ConfigMap key. A key must match `[-._a-zA-Z0-9]+` and be "
            f"neither `.` nor `..` (k8s.io/apimachinery `IsConfigMapKey`), so "
            f"no amount of quoting makes these work — rename them:",
            file=sys.stderr,
        )
        for name, why in illegal:
            print(f"  {name!r}: {why}", file=sys.stderr)
        print("       Leaving them out silently would delete those tenants "
              "from the ConfigMap behind a green light.", file=sys.stderr)
        return EXIT_VIOLATION

    sources = [(f"{p.name}={p}", p) for p in carriers]

    # ⛔ Ask the question about the ARTIFACT, not about the tree. The ConfigMap
    # key plane is flat, so only `carriers` can ever reach the exporter through
    # this path; validating the whole tree blocked deployable artifacts (a
    # second declaration under `examples/`, which never ships, refused the
    # build and told the operator the exporter would reject everything).
    # The nested files that do NOT ship are disclosed by `warn_nested` above,
    # which is the other half of the same honesty.
    verdict = tu.verdict_for({p.name: p for p in carriers})

    if verdict.outcome == tu.DUPLICATE:
        print(f"ERROR: {tu.CHECK_NAME} failed — refusing to assemble a ConfigMap "
              f"the exporter would reject in full.", file=sys.stderr)
        _forward(verdict)
        return tu.EXIT_FOR[verdict.outcome]

    if verdict.outcome != tu.CLEAN:
        print(f"ERROR: {verdict.reason}", file=sys.stderr)
        _forward(verdict)
        print("       This is 'could not measure', NOT 'measured clean'.",
              file=sys.stderr)
        return tu.EXIT_FOR[verdict.outcome]

    # ⛔ argv list, `shell=False`: every `--from-file=key=path` is ONE argv
    # element, so nothing re-parses a file name. That is the whole of #1796 —
    # a name holding a space, `(`, `` ` `` or `$( )` reaches kubectl byte for
    # byte instead of being split, evaluated, or fed to `basename` as a second
    # argument (which silently stripped the extension off the key).
    cmd = [
        "kubectl", "create", "configmap", args.name,
        *[f"--from-file={src}" for src, _p in sources],
        "-n", args.namespace, "--dry-run=client", "-o", "yaml",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=_KUBECTL_TIMEOUT_S)
    except FileNotFoundError:
        print("ERROR: `kubectl` not found on PATH. This target builds the "
              "manifest with `kubectl create configmap --dry-run=client` "
              "rather than hand-rolling a second YAML generator, so kubectl "
              "is a hard prerequisite.\n"
              "       -> install kubectl, or run this step on a runner that "
              "has it.", file=sys.stderr)
        return EXIT_CALLER_ERROR
    except subprocess.TimeoutExpired:
        # ⛔ Say what is ON DISK, not what we wish were. Nothing was written,
        # so a PREVIOUS run's `{args.output}` is still sitting there intact —
        # deliberately (see `_write_atomically`: a half file is worse than an
        # old one). The docs make assemble and `kubectl apply` two steps, so
        # an operator told the artifact was "not left behind" runs the second
        # step anyway and ships the OLD config believing it is this one.
        left = (f"{args.output} was NOT touched: it still holds a PREVIOUS "
                f"run's manifest, which may be stale"
                if Path(args.output).exists()
                else f"no {args.output} was written")
        print(f"ERROR: `kubectl create configmap` did not finish within "
              f"{_KUBECTL_TIMEOUT_S}s — nothing was written, so {left}.\n"
              f"       -> do NOT `kubectl apply` that file on the strength "
              f"of this run; re-run this step until it succeeds.",
              file=sys.stderr)
        return EXIT_CALLER_ERROR

    if proc.returncode != 0:
        print(f"ERROR: `kubectl create configmap` failed (rc="
              f"{proc.returncode}). Its own message follows:", file=sys.stderr)
        for line in (proc.stderr or "(no stderr)").rstrip().splitlines():
            print(f"  {line}", file=sys.stderr)
        return EXIT_VIOLATION

    # ⛔ Read back what we just produced. Everything above this line is an
    # intention; this is the only place the artifact itself answers.
    disagreements, measured = measure_artifact(proc.stdout, carriers)
    if disagreements:
        print(
            f"ERROR: the manifest `kubectl` produced does not match the "
            f"{len(carriers)} carrier(s) in {config_dir}. Nothing was "
            f"written. This is the argument list being mangled on its way "
            f"in (a `,`, a `\"` or an `=` in the path is enough — kubectl "
            f"splits the value before it ever looks at a file), and applying "
            f"it would deploy a ConfigMap that is not this tree:",
            file=sys.stderr,
        )
        for line in disagreements:
            print(f"  {line}", file=sys.stderr)
        print(f"       -> point --config-dir at a path holding none of "
              f"those characters, or copy the tree somewhere plainer.",
              file=sys.stderr)
        return EXIT_VIOLATION

    # The size rule, asked of the artifact rather than predicted from the
    # tree: `kubectl create --dry-run=client` does not apply it, so without
    # this the refusal lands at `kubectl apply`, on the whole object, naming
    # no file.
    total = sum(measured.values())
    if total > _MAX_CONFIGMAP_BYTES:
        print(
            f"ERROR: the manifest's values total {total} bytes. k8s "
            f"`ValidateConfigMap` sums them and rejects anything over "
            f"{_MAX_CONFIGMAP_BYTES} bytes, so `kubectl apply` of this "
            f"artifact fails on the OBJECT and names no file — refuse here, "
            f"where the files can be named. Largest:",
            file=sys.stderr,
        )
        for name, n in sorted(measured.items(), key=lambda kv: -kv[1])[:5]:
            print(f"  {name!r}: {n} bytes", file=sys.stderr)
        print(f"       -> split the tenants across more than one ConfigMap "
              f"(`make sharded-assemble`), or shrink the carriers.",
              file=sys.stderr)
        return EXIT_VIOLATION

    # ⚠️ WARN, not a refusal: this ceiling belongs to ONE of the two apply
    # modes, and blocking would stop a `--server-side` deploy that the API
    # server accepts.
    manifest_bytes = len(proc.stdout.encode("utf-8"))
    if manifest_bytes > _ANNOTATION_TOTAL_LIMIT:
        print(
            f"WARN: this manifest is {manifest_bytes} bytes, over the "
            f"{_ANNOTATION_TOTAL_LIMIT}-byte cap k8s puts on an object's "
            f"ANNOTATIONS — a quarter of the {_MAX_CONFIGMAP_BYTES}-byte "
            f"data limit, and checked first. A client-side `kubectl apply "
            f"-f` stores the whole object in `last-applied-configuration`, "
            f"so it answers `metadata.annotations: Too long` and names no "
            f"file.\n"
            f"       -> `kubectl apply --server-side -f` stores no such "
            f"annotation; or split the tenants (`make sharded-assemble`).\n"
            f"       ⚠️ Measured on this YAML; the annotation holds the same "
            f"object as JSON — the same order of magnitude, not equal.",
            file=sys.stderr,
        )

    output = Path(args.output)
    try:
        _write_atomically(output, proc.stdout)
    except OSError as exc:
        print(f"ERROR: could not write {output}: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    print(f"✓ {output} ({len(carriers)} files, no duplicate tenant)")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
