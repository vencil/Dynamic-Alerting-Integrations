#!/usr/bin/env python3
"""waveform_compile.py — fault-waveform pack 驗證 / 回讀 / 物化編譯器（ADR-030 決策層驗證 PR-1）

Single tool, three modes (P2):

  --check            schema-validate pack(s); SME-field gaps say 退回 SME,
                     platform-field gaps say 平台補填 (two-tier hard split).
  --render-readback  ASCII sparkline + ZH summary per signature, for the SME
                     read-back sign-off gate (P1 (c+); blind-write governance).
  --compile --out D  synthesize the always-on variant set (R2-1) and write
                     BOTH materializations + provenance metadata:
                       <id>.promtool.yaml  (a) promtool fixture fragment — reference only
                       <id>.vm.txt         (b) Prometheus import lines — catch-rate authority
                       <id>.metadata.json  variant / expects / seed / auto-adjustment trail

Deterministic: seeded PRNG only (--seed, fixed default), T0/STEP explicit
constants — same version + same seed ⇒ bitwise-identical outputs.

Governance gates:
  * `source: self-test-seed` without --allow-selftest → exit 1 (seeds must
    never enter catch-rate material).
  * `independent_of_rule_conversion` must be literally true (blind-write
    attestation, ADR-030 D2 anti-tautology).

Exit codes (scripts/tools/_lib_exitcodes.py):
  0  OK
  1  schema violation / governance gate (user fixes the pack)
  2  bad invocation / unreadable file / malformed YAML / jsonschema missing

Usage:
  python3 scripts/tools/dx/waveform_compile.py --check pack.yaml
  python3 scripts/tools/dx/waveform_compile.py --render-readback pack.yaml
  python3 scripts/tools/dx/waveform_compile.py --compile --out out/ --seed 1 pack.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
import _waveform_lib as wf  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_python import write_text_secure  # noqa: E402

try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover
    def try_utf8_stdout() -> None:  # type: ignore
        pass

# Repo-root-relative default: dx -> tools -> scripts -> <root>/docs/schemas/...
_DEFAULT_SCHEMA = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "..", "docs", "schemas", "waveform-pack.schema.json")
)


def _load_schema(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise wf.WaveformInputError(f"cannot load schema {path}: {exc}") from exc


def _validate_one(pack: dict, schema: dict, jsonschema_mod, allow_selftest: bool) -> list[dict]:
    issues = wf.validate_pack(pack, schema, jsonschema_mod)
    issues.extend(wf.selftest_gate_issues(pack, allow_selftest))
    issues.extend(wf.semantic_issues(pack))
    return issues


def _compile_one(pack: dict, out_dir: str, seed: int, fanout: int) -> list[str]:
    """Materialize one validated pack. Returns the written file paths."""
    series = wf.synthesize_pack(pack, seed=seed, fanout=fanout)
    pack_id = pack["pack"]["id"]
    written = []
    targets = {
        f"{pack_id}.promtool.yaml": wf.materialize_promtool(series),
        f"{pack_id}.vm.txt": wf.materialize_vm(series),
        f"{pack_id}.metadata.json": json.dumps(
            wf.build_metadata(pack, series, seed=seed, fanout=fanout),
            indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    }
    for name, content in targets.items():
        path = os.path.join(out_dir, name)
        write_text_secure(path, content)
        written.append(path)
    return written


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="fault-waveform pack 驗證/回讀/物化編譯器（ADR-030 PR-1；"
                    "schema: docs/schemas/waveform-pack.schema.json）")
    parser.add_argument("packs", nargs="+", help="waveform pack YAML path(s)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                      help="schema 驗證（SME 欄缺漏→退回 SME；平台欄→平台補填）")
    mode.add_argument("--render-readback", action="store_true",
                      help="輸出 ASCII sparkline + 中文摘要，供 SME 回讀簽核")
    mode.add_argument("--compile", action="store_true", dest="compile_mode",
                      help="物化：promtool fixture 片段 (a) + VM import 行 (b) + metadata")
    parser.add_argument("--out", help="--compile 的輸出目錄（必填）")
    parser.add_argument("--seed", type=int, default=wf.DEFAULT_SEED,
                        help=f"PRNG seed（決定性；預設 {wf.DEFAULT_SEED}）")
    parser.add_argument("--fanout", type=int, default=wf.DEFAULT_FANOUT,
                        help=f"fan-out 變體 series 數（預設 {wf.DEFAULT_FANOUT}）")
    parser.add_argument("--schema", default=_DEFAULT_SCHEMA,
                        help="JSON Schema 路徑（預設 docs/schemas/waveform-pack.schema.json）")
    parser.add_argument("--allow-selftest", action="store_true",
                        help="放行 source: self-test-seed（僅供工具自測；"
                             "self-test seed 不得進入 catch-rate 素材）")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="JSON 格式輸出（CI 整合）")
    args = parser.parse_args()

    if args.compile_mode and not args.out:
        parser.error("--compile requires --out DIR")
    if args.fanout < 1:
        parser.error("--fanout must be >= 1")

    # Lazy import: keep --help / bad-flag paths (the exit-code gate) working in
    # a jsonschema-less env (check_confd_schema.py precedent).
    try:
        import jsonschema
    except ImportError:
        print("ERROR: jsonschema not installed — `pip install jsonschema` "
              "(CI installs it in the Python Tests dep step).", file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        schema = _load_schema(args.schema)
    except wf.WaveformInputError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    all_issues: list[dict] = []
    outputs: list[str] = []
    for pack_path in args.packs:
        try:
            pack = wf.load_pack(pack_path)
        except wf.WaveformInputError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return EXIT_CALLER_ERROR

        issues = _validate_one(pack, schema, jsonschema, args.allow_selftest)
        for issue in issues:
            issue["pack"] = pack_path
        all_issues.extend(issues)
        if issues:
            continue  # never materialize / render an invalid pack

        if args.render_readback:
            print(wf.render_readback(pack))
        elif args.compile_mode:
            os.makedirs(args.out, exist_ok=True)
            try:
                written = _compile_one(pack, args.out, args.seed, args.fanout)
            except wf.WaveformInputError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return EXIT_CALLER_ERROR
            outputs.extend(written)

    if args.json_output:
        print(json.dumps({
            "check": "waveform-pack",
            "packs": args.packs,
            "seed": args.seed,
            "issues": all_issues,
            "outputs": [p.replace(os.sep, "/") for p in outputs],
            "pass": not all_issues,
        }, ensure_ascii=False, indent=2))
    else:
        for issue in all_issues:
            print(f"{issue['pack']}: {issue['message']}", file=sys.stderr)
        if all_issues:
            print(f"\n{len(all_issues)} violation(s) across {len(args.packs)} pack(s).",
                  file=sys.stderr)
        elif args.compile_mode:
            print(f"OK: {len(args.packs)} pack(s) materialized → {len(outputs)} file(s) "
                  f"under {args.out} (seed={args.seed}, fanout={args.fanout}, "
                  f"step={wf.STEP}s, T0={wf.T0})")
        elif args.check:
            print(f"OK: {len(args.packs)} pack(s) valid against waveform-pack.schema.json")

    return EXIT_VIOLATION if all_issues else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
