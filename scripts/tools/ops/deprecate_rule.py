#!/usr/bin/env python3
"""deprecate_rule.py — 規則/指標下架工具。

安全地將指定的 metric key 從平台中淘汰，三步自動化:
  Step 1: 在每個 _defaults.yaml／_defaults.yml 載體中設定該 metric 為 "disable"
  Step 2: 掃描所有 conf.d/*.yaml，移除殘留的 metric key
  Step 3: 產出下架報告 (含需手動處理的 ConfigMap 清理指引)

用法:
  # 預覽模式 (預設)
  python3 deprecate_rule.py mysql_slave_lag

  # 執行下架 (修改檔案)
  python3 deprecate_rule.py mysql_slave_lag --execute

  # 指定 conf.d 目錄
  python3 deprecate_rule.py mysql_slave_lag --config-dir /path/to/conf.d --execute

  # 同時處理多個 metric
  python3 deprecate_rule.py mysql_slave_lag mysql_innodb_buffer_pool --execute

注意:
  此工具處理 conf.d/ 層面的設定清理。Prometheus ConfigMap 中的
  Recording Rule / Alert Rule 需在下個 Release Cycle 手動移除。
"""

import sys
import os
import argparse
from pathlib import Path
import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import load_yaml_file as _lib_load_yaml  # noqa: E402
from _lib_python import write_text_secure  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_VIOLATION  # noqa: E402
from _lib_confd import (  # noqa: E402  (#1588 shared name predicates)
    defaults_files_in,
    has_yaml_extension,
    is_defaults_name,
    resolve_defaults_file,
    unusable_config_entries,
    unusable_reason,
    warn_nested,
)



def load_yaml_file(path):
    """安全載入 YAML 檔案，with warning on parse error.

    Delegates to ``_lib_python.load_yaml_file`` for the common path.
    Catches ``yaml.YAMLError`` on corrupt files (which the lib version
    intentionally does not swallow) so the deprecation workflow can
    continue with a warning instead of aborting.
    """
    try:
        result = _lib_load_yaml(path, default={})
        if result is None:
            return {}
        return result
    except (OSError, yaml.YAMLError) as e:
        print(f"  ⚠️  無法讀取 {safe_label(path)}: {safe_label(e)}")
        return None


def save_yaml_file(path, data, header_comment=""):
    """安全寫入 YAML 檔案。"""
    content = ""
    if header_comment:
        content += header_comment
    content += yaml.safe_dump(data, default_flow_style=False,
                              allow_unicode=True, sort_keys=False)
    write_text_secure(path, content)


def scan_for_metric(metric_key, config_dir):
    """掃描 conf.d/ 中所有引用指定 metric 的檔案。

    回傳: list of {filename, path, section, occurrences}
    """
    findings = []
    pattern_keys = [
        metric_key,
        f"{metric_key}_critical",
        f"custom_{metric_key}",
        f"custom_{metric_key}_critical",
    ]

    config_base = Path(config_dir)
    # #1339: flat by design here — but a hierarchical conf.d must not
    # look like an empty one. Name the files this scan cannot see.
    warn_nested(config_base, tool="deprecate_rule")
    entries = sorted(config_base.iterdir())
    # #1607: the `is_file()` filter below is a THIRD axis (neither the
    # extension axis of #1588 nor the recursion axis left alone above), and
    # it was silent. A directory named `notes.yaml/` or a broken symlink is
    # something the operator called configuration — reporting "✅ 未發現任何
    # 引用" for a tree containing one is the same false all-clear this tool
    # already gave on `.YAML`.
    #
    # ⛔ The naming is in `main`, NOT here. This function runs once PER
    # METRIC and one invocation takes several: measured, `deprecate_rule a b
    # c` printed the same two warnings three times. `warn_nested` above gets
    # away with sitting here only because it dedups per directory per
    # process internally.
    for entry in (
        p for p in entries
        if p.is_file() and has_yaml_extension(p.name)
    ):
        filename = entry.name
        path = str(entry)
        if filename.startswith('.'):
            continue

        data = load_yaml_file(path)
        if data is None:
            continue

        occurrences = []

        # Check defaults section (`defaults:` with no value is None, not {})
        defaults = data.get("defaults") or {}
        if not isinstance(defaults, dict):
            defaults = {}
        for pk in pattern_keys:
            if pk in defaults:
                occurrences.append(("defaults", pk, defaults[pk]))

        # Check tenants section
        tenants = data.get("tenants", {})
        for tenant_name, tenant_config in tenants.items():
            if not isinstance(tenant_config, dict):
                continue
            for pk in pattern_keys:
                if pk in tenant_config:
                    occurrences.append((f"tenants.{tenant_name}", pk, tenant_config[pk]))
            # Also check dimensional keys like "metric{label="value"}"
            for key, val in tenant_config.items():
                if metric_key in key and key not in pattern_keys:
                    occurrences.append((f"tenants.{tenant_name}", key, val))

        if occurrences:
            findings.append({
                "filename": filename,
                "path": path,
                "occurrences": occurrences,
            })

    return findings


def defaults_carriers(config_dir):
    """列出 conf.d 內所有 defaults 載體（`_defaults.yaml`／`.yml`，任意大小寫）。

    #1609: the exporter merges BOTH spellings into the defaults chain
    (`config_hierarchy.go:216`), so a write that touches only the one
    `resolve_defaults_file` picks leaves the threshold live in the other.
    The scan (`scan_for_metric`) already sees every carrier; the writer has
    to enumerate the same set or its report contradicts its action.

    Flat read, like every other scan in this tool — hence `warn_nested`
    (it dedups per directory per process, so this adds no second line).
    `defaults_files_in` takes already-listed names on purpose (see its
    docstring); listing here and filtering on `is_file()` keeps the set
    identical to what the scan could read. Sorted, so a directory carrying
    two spellings is processed in a deterministic order.
    """
    base = Path(config_dir)
    warn_nested(base, tool="deprecate_rule")
    entries = sorted(base.iterdir())
    return defaults_files_in(base, [e.name for e in entries if e.is_file()])


def disable_in_defaults(metric_key, config_dir, execute=False, *,
                        defaults_path=None):
    """在單一 defaults 載體中將 metric 設為 "disable"。

    Single-carrier entry point, kept for callers that unpack `ok, msg`.
    Without `defaults_path` it writes the one carrier
    `resolve_defaults_file` picks — which is exactly the #1609 blind spot,
    so the CLI flow goes through `disable_in_all_defaults` instead.
    """
    if defaults_path is None:
        defaults_path = resolve_defaults_file(Path(config_dir))
    return _disable_in_carrier(metric_key, Path(defaults_path), execute)


def disable_in_all_defaults(metric_key, config_dir, execute=False):
    """對每個 defaults 載體執行 `_disable_in_carrier`。

    Returns one `(path, ok, msg)` per carrier from `defaults_carriers`.
    With NO carrier present it returns the single canonical
    `resolve_defaults_file` entry (`_defaults.yaml 不存在`), so a tree
    without defaults reports exactly what it did before #1609.
    """
    base = Path(config_dir)
    carriers = defaults_carriers(base)
    if not carriers:
        carriers = [resolve_defaults_file(base)]
    return [(p, *_disable_in_carrier(metric_key, p, execute))
            for p in carriers]


def _disable_in_carrier(metric_key, defaults_path, execute=False):
    """在指定的 defaults 載體檔案中將 metric 設為 "disable"。"""
    defaults_path = str(defaults_path)
    if not Path(defaults_path).exists():
        return False, f"{Path(defaults_path).name} 不存在"

    data = load_yaml_file(defaults_path)
    if data is None:
        return False, f"無法讀取 {Path(defaults_path).name}"

    # `defaults:` with no value parses to None — an empty block, written
    # like one. Any OTHER non-mapping (list / scalar) is a broken file the
    # exporter cannot decode either; replacing it with a mapping would
    # silently discard its content (re-review, #1609) → refuse, by name.
    defaults = data.get("defaults")
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        return False, (f"defaults 不是 mapping（{type(defaults).__name__}），"
                       f"本工具不改寫，請先修檔")
    current_val = defaults.get(metric_key)

    if current_val == "disable":
        return True, f"已經是 disable 狀態"

    if execute:
        data["defaults"] = defaults
        data["defaults"][metric_key] = "disable"

        # Read original file to preserve header comment
        header = ""
        with open(defaults_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('#'):
                    header += line
                else:
                    break

        save_yaml_file(defaults_path, data, header)
        return True, f"已將 {metric_key} 設為 disable (原值: {current_val})"
    else:
        return True, f"將把 {metric_key} 從 {current_val} 改為 disable"


def remove_from_tenants(metric_key, config_dir, execute=False):
    """從所有 tenant 設定中移除殘留的 metric key。"""
    removed = []
    pattern_keys = [
        metric_key,
        f"{metric_key}_critical",
        f"custom_{metric_key}",
        f"custom_{metric_key}_critical",
    ]

    config_base = Path(config_dir)
    # #1339: second scan site — the guard must live where the scan does,
    # otherwise a hierarchical conf.d is silently empty on THIS path.
    warn_nested(config_base, tool="deprecate_rule")
    # #1607: same `is_file()` third axis as the reference scan, deliberately
    # NOT re-reported. `scan_for_metric` walks this identical directory and
    # always runs first in the CLI flow, so naming the same dropped entries
    # again would print each one twice per metric — and this tool is run
    # across several metrics in one invocation.
    for entry in sorted(
        p for p in config_base.iterdir()
        if p.is_file() and has_yaml_extension(p.name)
    ):
        filename = entry.name
        path = str(entry)
        if filename.startswith('_') or filename.startswith('.'):
            continue  # Skip _defaults.yaml

        data = load_yaml_file(path)
        if data is None:
            continue

        tenants = data.get("tenants", {})
        modified = False
        for tenant_name, tenant_config in tenants.items():
            if not isinstance(tenant_config, dict):
                continue
            keys_to_remove = []
            for key in tenant_config:
                if key in pattern_keys or (metric_key in key and '{' in key):
                    keys_to_remove.append(key)
            for key in keys_to_remove:
                val = tenant_config[key]
                removed.append((filename, tenant_name, key, val))
                if execute:
                    del tenant_config[key]
                    modified = True

        if modified and execute:
            header = ""
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.startswith('#'):
                        header += line
                    else:
                        break
            save_yaml_file(path, data, header)

    return removed


def main():
    """CLI entry point: 規則/指標下架工具。."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="規則/指標下架工具 — 三步安全淘汰 metric key"
    )
    parser.add_argument("metrics", nargs="+",
                        help="要下架的 metric key (例如 mysql_slave_lag)")
    parser.add_argument("--config-dir",
                        default="components/threshold-exporter/config/conf.d",
                        help="conf.d 目錄路徑")
    parser.add_argument("--execute", action="store_true",
                        help="實際執行下架 (預設只預覽)")

    args = parser.parse_args()

    if not Path(args.config_dir).is_dir():
        print(f"ERROR: config-dir not found: {args.config_dir}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    mode = "執行" if args.execute else "預覽"

    print(f"{'='*60}")
    print(f"🗑️  規則下架工具 — {mode}模式")
    print(f"{'='*60}\n")
    print(f"目標 Metrics: {', '.join(args.metrics)}")
    print(f"Config 目錄: {args.config_dir}\n")

    # #1607: named ONCE per invocation, before the per-metric loop — see
    # `scan_for_metric`, which is where the entries are actually dropped and
    # which runs once per metric.
    #
    # ⛔ `warn_nested` belongs HERE and not only in the scanning functions:
    # this line is itself a FLAT enumeration of the conf.d, and
    # `test_confd_enumeration_contract` reddened for exactly that — the guard
    # must live in the same scope as the scan, because "the import is in the
    # file" does nothing for the code path that gives the wrong answer. It
    # dedups per directory per process, so `scan_for_metric`'s call below
    # stays silent rather than repeating this one.
    #
    # ⛔ NO `try/except OSError` here, deliberately. The first version had one
    # and it was false robustness: `scan_for_metric` lists the same directory
    # two lines later with no guard of its own (and did so before #1607), so
    # swallowing the error here bought nothing except the impression that an
    # unlistable conf.d is handled. Measured — injecting EIO on this directory
    # made the run die inside `scan_for_metric` anyway, with the guard in
    # place. One failure mode, reported once, at the place that owns it.
    _base = Path(args.config_dir)
    warn_nested(_base, tool="deprecate_rule")
    _entries = sorted(_base.iterdir())
    for bad in unusable_config_entries(_entries):
        print(f"  ⚠️  略過 {safe_label(bad.name)}——{unusable_reason(bad)}",
              file=sys.stderr)

    # #1609: (metric, carrier, reason) for every defaults carrier the scan
    # listed but Step 1 did not write, plus every carrier Step 1 itself
    # reported as failed. Non-empty ⇒ the tool must NOT say 下架完成.
    incomplete = []

    for metric in args.metrics:
        print(f"\n{'─'*40}")
        print(f"📌 Processing: {metric}")
        print(f"{'─'*40}\n")

        # Step 1: 掃描
        findings = scan_for_metric(metric, args.config_dir)
        if findings:
            print(f"  📂 發現 {sum(len(f['occurrences']) for f in findings)} 處引用:")
            for f in findings:
                for section, key, val in f["occurrences"]:
                    print(f"     • {safe_label(f['filename'])} → "
                          f"[{safe_label(section)}] {safe_label(key)}: {safe_label(val)}")
        else:
            print(f"  ✅ 未發現任何引用")

        # Step 2: 在每個 defaults 載體中設為 disable (#1609: ALL carriers,
        # not the one `resolve_defaults_file` picks — the scan above lists
        # every carrier and the exporter reads every carrier).
        results = disable_in_all_defaults(metric, args.config_dir,
                                          execute=args.execute)
        for carrier, ok, msg in results:
            print(f"\n  Step 1: {safe_label(carrier.name)}")
            icon = "✅" if ok else "❌"
            print(f"  {icon} {safe_label(msg)}")

        # "found N / changed N" must agree before the summary may say 完成.
        # `found` is what the scan ITSELF reported as a defaults carrier —
        # the same output the operator just read — so a carrier the scan
        # named and Step 1 skipped is named again here, with its reason.
        with_defaults = {f["filename"] for f in findings
                         if any(sec == "defaults" for sec, _, _ in f["occurrences"])}
        # Only the exact-name carriers are the writer's; the rest follow the
        # exporter's own boundary rule (flat_scanner.go applyBoundaryRules):
        # a root-level `_`-prefixed file's `defaults:` IS merged into the
        # global defaults — on both planes, since the hierarchical plane
        # overlays the same flat merge (config_hierarchy.go, "merged at the
        # top level via scanDirFileHashes") — but this tool must not write
        # it (is_defaults_name) → incomplete, by name; a tenant file's
        # `defaults:` block is dropped with a WARN by the exporter → named
        # here, not counted (blind review, #1609).
        found = {n for n in with_defaults if is_defaults_name(n)}
        done = {p.name for p, ok, _ in results if ok}
        reasons = {p.name: msg for p, ok, msg in results if not ok}
        for name in sorted(found - done):
            incomplete.append((metric, name, reasons.get(name, "未寫入")))
        for p, ok, msg in results:
            if not ok and p.name not in found:
                incomplete.append((metric, p.name, msg))
        for name in sorted(with_defaults - found):
            if name.startswith("_"):
                incomplete.append((metric, name,
                                   "非 defaults 載體，本工具不寫入；exporter 會把 root 層任何 `_` 開頭檔的 "
                                   "defaults 併進全域（flat／hierarchical 皆然），需手動處理"))
            else:
                print(f"  ⚠️  {safe_label(name)} 的 defaults 區塊 exporter 會丟棄"
                      f"（只認 _defaults.yaml／.yml），本工具不寫入")

        # Step 3: 從 tenant configs 移除
        print(f"\n  Step 2: Tenant configs")
        removed = remove_from_tenants(metric, args.config_dir, execute=args.execute)
        if removed:
            for filename, tenant, key, val in removed:
                action = "已移除" if args.execute else "將移除"
                print(f"  🗑️  {action}: {safe_label(filename)} → "
                      f"{safe_label(tenant)}.{safe_label(key)} (值: {safe_label(val)})")
        else:
            print(f"  ✅ 無需清理 tenant configs")

        # Step 4: ConfigMap 指引
        print(f"\n  Step 3: Prometheus ConfigMap (手動)")
        print(f"  📋 下一個 Release Cycle 請手動移除:")
        print(f"     • Recording Rule: tenant:{metric}:* 或 tenant:custom_{metric}:*")
        print(f"     • Alert Rule: 引用上述 Recording Rule 的 Alert")
        print(f"     • Threshold Rule: tenant:alert_threshold:{metric}")

    # 總結
    print(f"\n{'='*60}")
    if incomplete:
        # #1609: a preview that cannot complete must not read as clean
        # either, so this branch runs in both modes.
        print("❌ 下架未完成：")
        for metric, carrier, reason in incomplete:
            print(f"   • {safe_label(metric)} → {safe_label(carrier)}："
                  f"{safe_label(reason)}")
        print(f"{'='*60}")
        sys.exit(EXIT_VIOLATION)
    if args.execute:
        print("✅ 下架完成！threshold-exporter 將在下次 reload 時生效。")
        print("📋 請在下個 Release Cycle 清理 Prometheus ConfigMap 中的對應規則。")
    else:
        print("💡 這是預覽模式。要實際執行，請加 --execute 參數。")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
