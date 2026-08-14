#!/usr/bin/env python3
"""Parse saved bench-record job logs -> per-night per-benchmark raw samples.

Source of truth: MCP get_job_logs dumps in the tool-results dir. Each file is
JSON {"job_id": N, "logs_content": "<full job log>"}.
No interpolation, no estimation: a night/benchmark that is absent stays absent.

⚠️ THIS SCRIPT IS NOT RUNNABLE AS-IS, AND THAT IS NOT A BUG TO FIX.
It is committed as the RECORD OF METHOD for the CSVs beside it: it needs a
directory of fetched job logs plus the `runs_page1.json` / `jobs.json` index
that the fetching session built, none of which are in this repo. Re-running it
means re-fetching 30 `bench-record` job logs first — and GitHub expires job
logs, so past some retention horizon this series cannot be rebuilt at all.
That is precisely why the OUTPUT is committed rather than the recipe alone.

Usage (only if you have re-fetched logs):  python3 parse_logs.py TOOLRES_DIR OUT_DIR
"""
import json, re, os, glob, sys, statistics as st

if len(sys.argv) != 3:
    sys.exit(__doc__)
TOOLRES, OUT = sys.argv[1], sys.argv[2]

BENCH_RE = re.compile(
    r"^(?P<ts>\S+)\s+(?P<name>Benchmark\S+?)-(?P<procs>\d+)\s+"
    r"(?P<iters>\d+)\s+(?P<nsop>[\d.]+)\s+ns/op"
    r"(?:\s+(?P<bop>\d+)\s+B/op)?(?:\s+(?P<aop>\d+)\s+allocs/op)?"
)

runs = {r["id"]: r for r in json.load(open(f"{OUT}/runs_page1.json"))}
jobs = json.load(open(f"{OUT}/jobs.json"))          # run_id(str) -> job meta
job2run = {v["job_id"]: int(k) for k, v in jobs.items()}

samples = []   # flat raw rows
nights = {}    # run_id -> night meta

for path in glob.glob(f"{TOOLRES}/mcp-github-get_job_logs-*.txt"):
    d = json.loads(open(path).read())
    jid = d["job_id"]
    if jid not in job2run:
        continue                      # not a bench-record job we asked for
    rid = job2run[jid]
    run = runs[rid]
    log = d["logs_content"]
    lines = log.split("\n")

    meta = dict(run_id=rid, run_number=run["num"], job_id=jid,
                created_utc=run["created"], night_utc=run["created"][:10],
                head_sha=run["sha"], conclusion=run["concl"],
                log_file=os.path.basename(path), log_lines=len(lines))

    # ---- runner / toolchain fingerprint -------------------------------
    for i, l in enumerate(lines):
        if "##[group]Runner Image\r" in l or l.rstrip().endswith("##[group]Runner Image"):
            for j in range(i + 1, min(i + 6, len(lines))):
                t = lines[j].split(" ", 1)[-1].strip()
                if t.startswith("Image:"):
                    meta["runner_image"] = t.split(":", 1)[1].strip()
                elif t.startswith("Version:") and "runner_image_version" not in meta:
                    meta["runner_image_version"] = t.split(":", 1)[1].strip()
        if "##[group]Runner Image Provisioner" in l:
            t = lines[i + 1].split(" ", 1)[-1].strip()
            if t.startswith("Version:"):
                meta["provisioner_version"] = t.split(":", 1)[1].strip()
        m = re.search(r"/opt/hostedtoolcache/go/([0-9.]+)/x64", l)
        if m:
            meta.setdefault("go_version", m.group(1))
        m = re.search(r"Runner name:\s*'([^']+)'", l)
        if m:
            meta["runner_name_log"] = m.group(1)
        m = re.search(r"Current runner version:\s*'([^']+)'", l)
        if m:
            meta["runner_agent_version"] = m.group(1)
        if "model name" in l.lower() or "Model name:" in l:
            meta.setdefault("cpu_model_line", l.strip()[:200])

    # ---- benchmark rows ----------------------------------------------
    n = 0
    for l in lines:
        mm = BENCH_RE.match(l.strip())
        if not mm:
            continue
        n += 1
        samples.append(dict(
            run_id=rid, run_number=run["num"], night_utc=run["created"][:10],
            head_sha=run["sha"], job_id=jid, log_file=os.path.basename(path),
            bench=mm.group("name"), procs=int(mm.group("procs")),
            sample_index=n, iters=int(mm.group("iters")),
            ns_op=float(mm.group("nsop")),
            b_op=int(mm.group("bop")) if mm.group("bop") else None,
            allocs_op=int(mm.group("aop")) if mm.group("aop") else None,
            log_line=l.strip(),
        ))
    meta["bench_rows"] = n
    meta["has_canary_rows"] = any("ControlCanary" in s for s in lines if "ns/op" in s)
    nights[rid] = meta

json.dump(samples, open(f"{OUT}/raw_samples.json", "w"), indent=1)
json.dump(nights, open(f"{OUT}/nights.json", "w"), indent=1)

import csv
with open(f"{OUT}/raw_samples.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(samples[0].keys()))
    w.writeheader()
    for s in samples:
        w.writerow(s)

print("nights parsed:", len(nights), " raw samples:", len(samples))
per = {}
for s in samples:
    per.setdefault(s["night_utc"], set()).add(s["bench"])
for nt in sorted(per):
    print(nt, "benches:", len(per[nt]))
print("canary rows present in any log:",
      sum(1 for m in nights.values() if m["has_canary_rows"]))
