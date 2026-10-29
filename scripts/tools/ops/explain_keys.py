#!/usr/bin/env python3
"""explain_keys.py — `da-tools explain-keys`: per tenant, who reads each key and what it does.

ADR-038 decision 9: let users check and test their own config. For each
tenant of a conf.d, every key of its config is listed with

* where it comes from: da-guard effective's layer and file;
* who reads it: the exporter (measured by da-guard served-values), and the
  readers the reserved-key registry declares for that layer (route
  generator, custom-alert compiler, Policy-as-Code, annotation), plus
  Policy-as-Code for a key a `_policies` rule names as `target`;
* the verdict (read / unread / undecidable) with its
  severity and reason — the shared verdict, `_lib_key_verdict.judge_tree`,
  not a reading of this tool's own;
* what a `disable` does (effective / partial / noop), by resolving the tree
  again without it (da-guard served-values --disable-effects);
* the value as written (`value`) and what /metrics serves for it (`served`).

`--assert FILE` compares the result with an expected document of the same
shape and fails (exit 1) on any difference, so a CI can pin what a tenant's
config means — like `amtool config routes test` or `promtool test rules`.
The expected file lists, per tenant it names, every key that tenant has;
each key entry is compared on the fields it gives (leave out `value` or
`served` to pin only the verdict). Tenants it does not name are not compared.

The tool reports what it finds; whether a value is what you meant is yours
to check here.

Exit codes (dev-rules §13, `_lib_exitcodes`):
  0  done (and, with --assert, the result matches)
  1  --assert: the result differs from the expected document
  2  caller error: --config-dir missing, no da-guard or da-guard failed, an
     --assert file that cannot be read, a --tenant the tree does not have,
     or a verdict that cannot be given (no root platform file was read, so
     the threshold keys are `undecidable`; ADR-038 decision 7)

`--json`: stdout is exactly one JSON document on every path the tool runs
(schema ``da-tools.explain-keys/v1``; an argument argparse refuses exits 2
before that, as everywhere). A caller error carries ``status:
"caller_error"`` and ``reason`` with ``tenants`` empty; undecidable threshold
keys exit 2 with ``status: "ok"`` and the whole result, ``undecidable``
true. The `--assert` file is refused (exit 2) when an entry has a field this
document does not have or a tenant lists a key twice: either would let it
pass without comparing what it seems to.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.dirname(_HERE)
for _p in (_TOOLS, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _lib_key_verdict as kv  # noqa: E402
import _lib_tenant_values as tv  # noqa: E402
from _lib_exitcodes import (  # noqa: E402
    EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION, arm_crash_envelope, disarm_crash_envelope,
)
from _lib_io import YamlFileError, classify_path, safe_label  # noqa: E402
import _lib_parse_cache  # noqa: E402
import _lib_parse_tree  # noqa: E402
from _lib_parse_tree import ParseTreeError  # noqa: E402
from _lib_python import detect_cli_lang, positive_seconds  # noqa: E402

SCHEMA = "da-tools.explain-keys/v1"
DEFAULT_TIMEOUT = tv.DEFAULT_TIMEOUT
#: What this tool writes at the top level; an expected file may carry them
#: (it is often this tool's own output) but only `tenants` is compared.
TOP_FIELDS = frozenset({"schema", "status", "at", "undecidable", "tenants", "findings",
                        "assert_differences"})
FIELDS = ("layer", "file", "read_by", "verdict", "severity", "reason", "disable",
          "served_family", "value", "served")


class ExplainError(Exception):
    """A caller error: exit 2 with this reason."""


def _entry(v: kv.KeyVerdict) -> dict[str, Any]:
    return {"key": v.key, "layer": v.layer, "file": v.file, "read_by": list(v.read_by),
            "verdict": v.verdict, "severity": v.severity, "reason": v.reason,
            "disable": v.disable, "served_family": list(v.served_family),
            "value": v.value, "served": v.served}


def explain(conf_d: Path, tenants: list[str], at: Optional[str], binary: Optional[str],
            timeout: float) -> dict[str, Any]:
    """The explain-keys document. *binary* / *timeout* (the CLI's
    `--da-guard-binary` / `--timeout`) are this run's da-guard for every
    read — the conf.d listing `_lib_confd` takes on its behalf as well as
    the tree and served-values reads (`_lib_parse_tree.use_da_guard`, #3206)."""
    with _lib_parse_tree.use_da_guard(binary, timeout):
        return _explain(conf_d, tenants, at, binary, timeout)


def _explain(conf_d: Path, tenants: list[str], at: Optional[str], binary: Optional[str],
             timeout: float) -> dict[str, Any]:
    conf_d_problem = classify_path(conf_d, "dir", flag="--config-dir")  # #3276
    if conf_d_problem is not None:
        raise ExplainError(conf_d_problem.message)
    # The verdict takes `_policies` from the root `_defaults.yaml` only, as
    # Policy-as-Code does; say which subdirectory ones it does not count.
    # #3206: ONE reader — one parse-tree document — for the note and the verdict.
    reader = _lib_parse_cache.ConfdReader(conf_d, binary=binary, timeout=timeout)
    try:
        kv.note_root_only_policies(conf_d, reader)
        verdict = kv.judge_tree(conf_d, at=at, binary=binary, timeout=timeout, reader=reader)
    except (tv.DaGuardNotFoundError, tv.DaGuardError, tv.ParseFailedError) as e:
        raise ExplainError(f"da-guard: {e}") from e
    except ParseTreeError as e:
        # The root `_policies` read (ADR-036 step 4): no usable parse-tree.
        raise ExplainError(f"da-guard parse-tree: {' '.join(str(e).split())}") from e
    except YamlFileError as e:
        raise ExplainError(str(e)) from e
    except ValueError as e:
        problems = getattr(e, "problems", None)  # policy_engine.InvalidPolicyRules
        if problems is None:
            raise
        raise ExplainError("\n".join([str(e), *problems])) from e
    missing = sorted(set(tenants) - set(verdict.keys))
    if missing:
        raise ExplainError(f"no such tenant in {conf_d}: {', '.join(missing)}")
    shown = tenants or sorted(verdict.keys)
    return {
        "schema": SCHEMA,
        "status": "ok",
        "at": at,
        "undecidable": verdict.undecidable,
        "tenants": {t: [_entry(v) for v in verdict.keys[t]] for t in shown},
        "findings": [dict(f._asdict(), tenants=list(f.tenants), served_family=list(f.served_family),
                          served_from=list(f.served_from))
                     for f in verdict.findings
                     if not tenants or set(f.tenants) & set(tenants)],
    }


def compare(doc: dict[str, Any], expected: Any) -> list[str]:
    """Every difference between `doc` and the expected document, one line each."""
    if not isinstance(expected, dict) or not isinstance(expected.get("tenants"), dict):
        raise ExplainError("--assert: the expected document needs a `tenants` mapping")
    unknown_top = sorted(set(expected) - TOP_FIELDS)
    if unknown_top:
        raise ExplainError(f"--assert: unknown top-level field(s) {unknown_top}; only `tenants` is compared")
    out = []
    for tenant, want in sorted(expected["tenants"].items()):
        got = {e["key"]: e for e in doc["tenants"].get(tenant, [])}
        if tenant not in doc["tenants"]:
            out.append(f"{tenant}: not in the result")
            continue
        if not isinstance(want, list) or not all(isinstance(e, dict) and isinstance(e.get("key"), str)
                                                  for e in want):
            raise ExplainError(f"--assert: tenants.{tenant} must be a list of entries with a string `key`")
        wanted: dict[str, dict] = {}
        for e in want:
            unknown = sorted(set(e) - {"key", *FIELDS})
            if unknown:
                # A misspelt field would otherwise compare nothing and pass.
                raise ExplainError(f"--assert: tenants.{tenant}: {e['key']}: unknown field(s) {unknown}")
            if e["key"] in wanted:
                raise ExplainError(f"--assert: tenants.{tenant}: {e['key']} is listed twice")
            wanted[e["key"]] = e
        for key in sorted(set(got) - set(wanted)):
            out.append(f"{tenant}: {key}: in the result, not expected")
        for key in sorted(set(wanted) - set(got)):
            out.append(f"{tenant}: {key}: expected, not in the result")
        for key in sorted(set(wanted) & set(got)):
            for field in FIELDS:
                if field in wanted[key] and wanted[key][field] != got[key][field]:
                    out.append(f"{tenant}: {key}: {field}: expected {json.dumps(wanted[key][field])}, "
                               f"got {json.dumps(got[key][field])}")
    return out


def render_text(doc: dict[str, Any]) -> str:
    lines = []
    for tenant, rows in doc["tenants"].items():
        lines.append(safe_label(tenant))
        table = [("key", "source", "read_by", "verdict", "severity", "disable")]
        for e in rows:
            table.append((safe_label(e["key"]), safe_label(e["file"] or "—"), ",".join(e["read_by"]) or "—",
                          e["verdict"], e["severity"] or "—", e["disable"] or "—"))
        widths = [max(len(str(r[i])) for r in table) for i in range(len(table[0]))]
        for r in table:
            lines.append("  " + "  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())
        lines.append("")
    if doc["findings"]:
        lines.append("Findings:")
        for f in doc["findings"]:
            who = ", ".join(safe_label(t) for t in f["tenants"][:5]) + (
                f" (+{len(f['tenants']) - 5} more)" if len(f["tenants"]) > 5 else "")
            why = f" ({f['reason']})" if f["reason"] else ""
            fam = ("; still served: " + ", ".join(
                f"{safe_label(k)} (from {safe_label(src or '?')})"
                for k, src in zip(f["served_family"], f["served_from"])) if f["served_family"] else "")
            lines.append(f"  {f['severity']}: {f['kind']}: {safe_label(f['key'])} in "
                         f"{safe_label(f['file'] or '?')}{why}{fam} — {who or 'no tenant'}")
    if doc["undecidable"]:
        lines.append("No root platform file was read: threshold keys are undecidable (ADR-038 decision 7).")
    return "\n".join(lines).rstrip("\n")


# ── CLI ──────────────────────────────────────────────────────────────────

_HELP = {
    "en": {
        "description": ("For each tenant of a conf.d, list every key of its config: where it comes "
                        "from, which readers read it, the verdict, what a `disable` does, the value "
                        "as written and what /metrics serves (ADR-038 decision 9)."),
        "epilog": ("Exit codes: 0 done (and --assert matches), 1 --assert differs, 2 caller error "
                   "(no --config-dir, no da-guard, da-guard failed, unreadable --assert file, "
                   "unknown --tenant, or undecidable threshold keys). "
                   f"--json: one document on stdout, schema {SCHEMA}."),
        "config_dir": "Path to the conf.d/ root.",
        "tenant": "Only this tenant (repeatable). Default: every tenant.",
        "at": "Instant to read schedules and time-boxes at, RFC3339. Default: now.",
        "assert": "Expected document (same shape as --json); exit 1 on any difference.",
        "json": "Print the result as one JSON document.",
        "binary": "Path to da-guard (default: $DA_GUARD_BINARY, then da-guard on $PATH).",
        "timeout": f"Seconds each da-guard run may take (default {DEFAULT_TIMEOUT}).",
    },
    "zh": {
        "description": ("對 conf.d 的每個租戶，逐鍵列出：來源層與檔案、哪些讀取端讀它、判決、"
                        "`disable` 的作用、寫下的值與 /metrics 實際送出的值（ADR-038 決定 9）。"),
        "epilog": ("結束碼：0 完成（--assert 時結果相符）、1 --assert 不符、2 呼叫錯誤（--config-dir "
                   "不存在、找不到 da-guard 或 da-guard 失敗、--assert 檔讀不了、--tenant 不存在，"
                   f"或閾值鍵無從判斷）。--json：stdout 恰好一份 JSON，schema {SCHEMA}。"),
        "config_dir": "conf.d/ 根目錄。",
        "tenant": "只列這個租戶（可重複）。預設：全部租戶。",
        "at": "讀排程與時效的時間點，RFC3339。預設：現在。",
        "assert": "預期結果（與 --json 同形狀）；有任何不同即結束碼 1。",
        "json": "以一份 JSON 輸出結果。",
        "binary": "da-guard 路徑（預設依序：$DA_GUARD_BINARY、$PATH 上的 da-guard）。",
        "timeout": f"每次執行 da-guard 的上限秒數（預設 {DEFAULT_TIMEOUT}）。",
    },
}


def build_parser() -> argparse.ArgumentParser:
    h = _HELP["zh" if detect_cli_lang() == "zh" else "en"]
    p = argparse.ArgumentParser(prog="da-tools explain-keys", description=h["description"],
                                epilog=h["epilog"])
    p.add_argument("--config-dir", required=True, help=h["config_dir"])
    p.add_argument("--tenant", action="append", default=[], help=h["tenant"])
    p.add_argument("--at", default=None, help=h["at"])
    p.add_argument("--assert", dest="assert_file", default=None, metavar="FILE", help=h["assert"])
    p.add_argument("--json", action="store_true", help=h["json"])
    p.add_argument("--da-guard-binary", default=None, help=h["binary"])
    p.add_argument("--timeout", type=positive_seconds, default=DEFAULT_TIMEOUT, help=h["timeout"])
    return p


def _fail(reason: str, as_json: bool) -> int:
    first, *rest = reason.split("\n")
    print(f"ERROR: {safe_label(first)}", file=sys.stderr)
    for line in rest:
        print(f"  {safe_label(line)}", file=sys.stderr)
    if as_json:
        print(json.dumps({"schema": SCHEMA, "status": "caller_error", "reason": reason,
                          "at": None, "undecidable": False, "tenants": {}, "findings": []},
                         ensure_ascii=False))
    return EXIT_CALLER_ERROR


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.json:
        # #3094: a crash in --json mode still prints one envelope, with the
        # keys _fail's caller_error document carries
        arm_crash_envelope(schema=SCHEMA, at=None, undecidable=False, tenants={}, findings=[])
    try:
        expected = None
        if args.assert_file is not None:
            try:
                with open(args.assert_file, encoding="utf-8") as f:
                    expected = json.load(f)
            except (OSError, UnicodeDecodeError, ValueError) as e:
                raise ExplainError(f"--assert: cannot read {args.assert_file}: {e}") from e
        # #3276: the caller's string, before Path('') becomes '.'.
        config_dir_problem = classify_path(args.config_dir, "dir", flag="--config-dir")
        if config_dir_problem is not None:
            raise ExplainError(config_dir_problem.message)
        doc = explain(Path(args.config_dir), args.tenant, args.at, args.da_guard_binary, args.timeout)
        diffs = compare(doc, expected) if expected is not None else []
    except ExplainError as e:
        return _fail(str(e), args.json)
    except Exception as e:  # noqa: BLE001 — every failure is exit 2 with a reason, never a traceback
        return _fail(f"explain-keys could not finish: {type(e).__name__}: {' '.join(str(e).split())}",
                     args.json)
    if args.json:
        doc["assert_differences"] = diffs
        print(json.dumps(doc, ensure_ascii=False, default=str))
        disarm_crash_envelope()  # the document is out; a late crash must not add a second
    else:
        print(render_text(doc))
        for d in diffs:
            print(f"ASSERT: {safe_label(d)}", file=sys.stderr)
    if doc["undecidable"]:
        print("ERROR: no root platform file was read; threshold keys are undecidable "
              "(ADR-038 decision 7)", file=sys.stderr)
        return EXIT_CALLER_ERROR
    return EXIT_VIOLATION if diffs else EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
