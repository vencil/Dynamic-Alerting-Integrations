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

⛔ 不自己重寫 kubectl 的 YAML 產生器：`kubectl` 本來就是這條 recipe 的既有
依賴，第二份產生器只會與它漂移。

⛔ **這支擋的是「產物不合法」，一律在寫出產物之前；它不是內容政策。**
上面三軸之後，同一個問題的其餘幾面各自有述詞（每一面都是從別人的權威
轉寫的，不是本檔發明的品味）：檔名能不能當 key（`configmap_key_problem`
← `IsConfigMapKey`）、`--from-file=` 的來源字串 kubectl 解不解得開
（`from_file_source_problem` ← `ParseFileSource`；`env=prod/` 這種目錄名
以前會讓 kubectl 拒絕整條命令而誰也不點名）、`data` 的總位元組會不會超過
API server 的上限（← `ValidateConfigMap`）。

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
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_TOOLS = _THIS_DIR.parent / "tools"
sys.path.insert(0, str(_TOOLS))
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK,
    EXIT_VIOLATION,
    EXIT_CALLER_ERROR,
)
from _lib_confd import (  # noqa: E402
    has_yaml_extension,
    is_hidden_name,
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
    """
    if len(os.fsencode(name)) > _MAX_KEY_BYTES:
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


# ── `--from-file` source legality (#1796, second half) ───────────────
#
# ⛔ NOT from memory. Transcribed from the authority,
# k8s.io/kubectl `pkg/generate/generate.go`:
#
#   func ParseFileSource(source string) (keyName, filePath string, err error) {
#       numSeparators := strings.Count(source, "=")
#       switch {
#       case numSeparators == 0:              return path.Base(source), source, nil
#       case numSeparators == 1 && HasPrefix(source, "="): ... "key name ... missing"
#       case numSeparators == 1 && HasSuffix(source, "="): ... "file path ... missing"
#       case numSeparators > 1:
#           return "", "", errors.New("key names or file paths cannot contain '='")
#       ...
#
# We always emit the `key=path` form, and `_CONFIGMAP_KEY_RE` already denies
# `=` in the key — so the only way to reach `numSeparators > 1` is an `=` in
# the PATH, i.e. in `--config-dir`. `env=prod/` and a Jenkins matrix axis
# directory (`axis=value/`) are both ordinary directory names.
_MAX_FROM_FILE_SEPARATORS = 1


def from_file_source_problem(source: str) -> str | None:
    """Why `kubectl` cannot parse `--from-file=<source>`, or `None` if it can.

    ⛔ Asked about the string we are ABOUT TO BUILD, not about the file name
    alone. kubectl's own refusal for this case says "key names or file paths
    cannot contain '='" and names neither, so an operator whose `CONFDIR`
    holds `env=prod/` reads it as an accusation against their file names and
    goes looking in the wrong place.
    """
    extra = source.count("=") - _MAX_FROM_FILE_SEPARATORS
    if extra > 0:
        return (f"holds {extra + _MAX_FROM_FILE_SEPARATORS} '=' characters; "
                f"kubectl splits `--from-file=key=path` on '=' and refuses "
                f"more than one")
    return None


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
            f"       That is the built-in `CONFDIR` default. Its tenants "
            f"(`db-a` / `db-b`) are reference templates — they ship in "
            f"neither the chart nor the image, and they are almost certainly "
            f"not yours. `kubectl apply` of this artifact REPLACES the live "
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
    for p in unusable:
        print(f"WARN: {p.name!r} in {config_dir} {unusable_reason(p)} — it "
              f"carries a config name but NOTHING was read from it, so it is "
              f"absent from the ConfigMap and its tenants have no alerting.",
              file=sys.stderr)

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

    # #1796, the other half: the file name was checked, the SOURCE STRING we
    # build out of it was not. `--from-file=` takes `key=path`, so an `=`
    # anywhere in `--config-dir` makes kubectl refuse the whole command with
    # a message that blames "key names or file paths" and names neither.
    # `env=prod/` and a Jenkins matrix axis directory are ordinary names.
    sources = [(f"{p.name}={p}", p) for p in carriers]
    unparseable = [(src, p, why) for src, p in sources
                   if (why := from_file_source_problem(src)) is not None]
    if unparseable:
        print(
            f"ERROR: {len(unparseable)} --from-file argument(s) cannot be "
            f"parsed by kubectl. The offending character is in the PATH, not "
            f"in the file names — rename the directory, or point "
            f"--config-dir at a copy whose path holds no '=':",
            file=sys.stderr,
        )
        for _src, p, why in unparseable:
            print(f"  {str(p)!r}: {why}", file=sys.stderr)
        return EXIT_VIOLATION

    # A ConfigMap the API server will reject for size is not an artifact, and
    # that rejection lands one step later — at `kubectl apply`, on a whole
    # object, naming no file. ⚠️ Summed over the FILE BYTES because that is
    # the quantity `ValidateConfigMap` bounds; anything kubectl decides to
    # carry as `binaryData` is base64'd and therefore larger on the wire, so
    # this bound can only under-report, never false-red.
    sizes = [(p, p.stat().st_size) for p in carriers]
    total = sum(n for _p, n in sizes)
    if total > _MAX_CONFIGMAP_BYTES:
        print(
            f"ERROR: the {len(carriers)} carrier(s) total {total} bytes. k8s "
            f"`ValidateConfigMap` sums the `data` values and rejects anything "
            f"over {_MAX_CONFIGMAP_BYTES} bytes, so `kubectl apply` of this "
            f"artifact fails on the OBJECT and names no file — refuse here, "
            f"where the files can be named. Largest:",
            file=sys.stderr,
        )
        for p, n in sorted(sizes, key=lambda kv: -kv[1])[:5]:
            print(f"  {p.name!r}: {n} bytes", file=sys.stderr)
        print(f"       -> split the tenants across more than one ConfigMap "
              f"(`make sharded-assemble`), or shrink the carriers.\n"
              f"       ⚠️ This is a LOWER bound on what the cluster will "
              f"refuse: the serialized request carries the manifest, not "
              f"just these bytes.", file=sys.stderr)
        return EXIT_VIOLATION

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
