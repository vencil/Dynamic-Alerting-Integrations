#!/usr/bin/env python3
"""trufflehog_to_sarif.py — convert trufflehog JSON findings to SARIF 2.1.0
and apply the L2 verified/unverified merge-block policy (#445 AC ii).

Why this exists
---------------
trufflehog has no native SARIF output (only JSON + a GitHub-Actions
annotation format). The L2 server-side scan (`secret-scan.yml`) needs
SARIF to feed GitHub Code Scanning (Security tab + alert history), so
this converter bridges the gap.

It also owns the verified/unverified POLICY so the workflow doesn't
have to hand-roll jq:

  - A "verified" finding (trufflehog confirmed the credential is live)
    → SARIF level `error` AND this script exits 1 → the workflow step
    fails → PR merge blocked. This is the L2 hard gate.
  - An "unverified" / "unknown" finding (regex/entropy match, but the
    live-check either was not attempted or failed) → SARIF level
    `warning`, script still exits 0 → surfaced in Code Scanning but
    does NOT block the PR.

Input format
------------
trufflehog `--json` emits NEWLINE-DELIMITED JSON (one finding object
per line), NOT a single JSON array. Non-JSON lines (should not occur
with `--json`, but defensive) are skipped with a stderr note.

Each finding object of interest carries:
  DetectorName       — e.g. "AWS", "GitHubToken"  → SARIF ruleId
  Verified           — bool                       → policy + level
  VerificationError  — present when a live-check was attempted+failed
  SourceMetadata.Data.{Git,Filesystem,...}.{file,line,commit}

Usage
-----
  trufflehog_to_sarif.py --input findings.json --output results.sarif
  trufflehog_to_sarif.py --input findings.json --output results.sarif \\
      --tool-version 3.95.3 --scan-mode pr

Exit codes
----------
  0 — converted OK; no VERIFIED findings (PR may proceed; unverified
      findings, if any, are warnings only)
  1 — converted OK; at least one VERIFIED finding → block the PR
  2 — usage / IO error (input missing, output unwritable)

Issue #445 AC ii — L2 layer of the L0/L1/L2/L3 multi-layer.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SARIF_SCHEMA = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
SARIF_VERSION = "2.1.0"

EXIT_OK = 0
EXIT_VERIFIED_FINDING = 1
EXIT_USAGE = 2


def _extract_location(finding: dict[str, Any]) -> tuple[str, int]:
    """Pull (file_path, line) out of a trufflehog finding.

    trufflehog nests location data under SourceMetadata.Data.<SourceType>,
    where SourceType is Git / Filesystem / Github / etc. — each with a
    slightly different shape. Probe the known shapes; fall back to a
    sentinel so a schema surprise degrades to a still-valid SARIF result
    rather than a crash.
    """
    meta = (finding.get("SourceMetadata") or {}).get("Data") or {}
    # meta is e.g. {"Git": {"file": "...", "line": 12, "commit": "..."}}
    for source_type, payload in meta.items():
        if not isinstance(payload, dict):
            continue
        file_path = payload.get("file") or payload.get("Filename") or ""
        line = payload.get("line") or payload.get("Line") or 0
        if file_path:
            try:
                line_int = int(line)
            except (TypeError, ValueError):
                line_int = 0
            # SARIF region.startLine must be >= 1.
            return file_path, max(line_int, 1)
    return "<unknown>", 1


def _classify(finding: dict[str, Any]) -> str:
    """Return 'verified' or 'unverified' for a finding.

    trufflehog has 3 internal states (verified / unverified / unknown);
    the #445 AC ii policy is 2-bucket: only a confirmed-live credential
    (Verified == true) blocks. Everything else is a warning.
    """
    return "verified" if finding.get("Verified") is True else "unverified"


def convert(findings: list[dict[str, Any]], tool_version: str) -> tuple[dict[str, Any], int]:
    """Build a SARIF document from trufflehog findings.

    Returns (sarif_dict, verified_count).
    """
    results: list[dict[str, Any]] = []
    rule_ids: dict[str, None] = {}  # ordered set of detector names
    verified_count = 0

    for finding in findings:
        detector = finding.get("DetectorName") or "UnknownDetector"
        rule_ids.setdefault(detector, None)
        kind = _classify(finding)
        if kind == "verified":
            verified_count += 1
            level = "error"
            verdict = "VERIFIED — credential confirmed live"
        else:
            level = "warning"
            verr = finding.get("VerificationError")
            verdict = (
                "unverified — live-check failed/errored"
                if verr
                else "unverified — no live-check performed"
            )

        file_path, line = _extract_location(finding)
        results.append({
            "ruleId": detector,
            "level": level,
            "message": {
                "text": f"Potential {detector} secret ({verdict}). "
                        f"Rotate first if real — see "
                        f"docs/internal/secret-leak-remediation-sop.md."
            },
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": file_path},
                    "region": {"startLine": line},
                },
            }],
        })

    sarif = {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [{
            "tool": {
                "driver": {
                    "name": "TruffleHog",
                    "informationUri": "https://github.com/trufflesecurity/trufflehog",
                    "version": tool_version,
                    "rules": [
                        {
                            "id": rid,
                            "name": rid,
                            "shortDescription": {"text": f"{rid} secret detector"},
                        }
                        for rid in rule_ids
                    ],
                },
            },
            "results": results,
        }],
    }
    return sarif, verified_count


def parse_ndjson(raw: str) -> list[dict[str, Any]]:
    """Parse trufflehog's newline-delimited JSON. Skip blank / non-JSON
    lines defensively (with a stderr note) rather than aborting — a
    stray log line must not sink the whole conversion."""
    findings: list[dict[str, Any]] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            print(f"  skip: line {lineno} is not JSON ({line[:60]}...)",
                  file=sys.stderr)
            continue
        if isinstance(obj, dict):
            findings.append(obj)
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert trufflehog JSON findings to SARIF 2.1.0 (#445 AC ii).",
    )
    parser.add_argument("--input", required=True,
                        help="trufflehog --json output file (NDJSON)")
    parser.add_argument("--output", required=True,
                        help="SARIF file to write")
    parser.add_argument("--tool-version", default="unknown",
                        help="trufflehog version string for the SARIF tool.driver")
    parser.add_argument("--scan-mode", default="pr", choices=["pr", "full"],
                        help="pr (diff scan) or full (nightly) — affects only the summary text")
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.is_file():
        print(f"❌ input file not found: {in_path}", file=sys.stderr)
        return EXIT_USAGE

    findings = parse_ndjson(in_path.read_text(encoding="utf-8", errors="replace"))
    sarif, verified_count = convert(findings, args.tool_version)

    try:
        Path(args.output).write_text(
            json.dumps(sarif, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        print(f"❌ could not write SARIF to {args.output}: {e}", file=sys.stderr)
        return EXIT_USAGE

    total = len(findings)
    unverified_count = total - verified_count
    print(f"▸ trufflehog {args.scan_mode}-scan: {total} finding(s) "
          f"— {verified_count} verified, {unverified_count} unverified")
    print(f"  SARIF written: {args.output}")

    if verified_count > 0:
        print(
            f"❌ {verified_count} VERIFIED secret(s) — blocking.\n"
            "   This is a confirmed-live credential. Follow the SOP NOW:\n"
            "   docs/internal/secret-leak-remediation-sop.md — ROTATE FIRST.",
            file=sys.stderr,
        )
        return EXIT_VERIFIED_FINDING

    if unverified_count > 0:
        print(f"⚠️  {unverified_count} unverified finding(s) — surfaced in "
              "Code Scanning as warnings, NOT blocking this PR. Review them; "
              "add `.trufflehogignore` / `# trufflehog:ignore` if false positive.")
    else:
        print("✅ no secrets detected.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
