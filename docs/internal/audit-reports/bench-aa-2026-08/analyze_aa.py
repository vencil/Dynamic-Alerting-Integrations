#!/usr/bin/env python3
"""重算本目錄兩份資料的每一個宣稱。無參數，從 repo 根或本目錄跑皆可。

    python3 docs/internal/audit-reports/bench-aa-2026-08/analyze_aa.py

⛔ 本腳本**只讀本目錄的檔**，不連網、不重跑 benchmark。它回答的是
「README 上的數字能不能從收進來的資料重算出來」，不是「今天重跑會得到什麼」。
"""
import json
import math
import os
import statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))


def aa_sessions():
    doc = json.load(open(os.path.join(HERE, "aa-sessions.json"), encoding="utf-8"))
    for s in doc["sessions"]:
        per = {}
        for name, f in s["files"].items():
            r, side = name[1:].rsplit("_", 1)
            per.setdefault(int(r), {})[side] = f["samples"]
        ratios, ns, first = [], [], []
        for r in sorted(per):
            sides = per[r]
            if "A" not in sides or "B" not in sides:
                continue
            a = st.median(x["ns_op"] for x in sides["A"])
            b = st.median(x["ns_op"] for x in sides["B"])
            ratios.append((r, 100 * (b / a - 1)))
            ns += [x["n"] for v in sides.values() for x in v]
        for name, f in sorted(s["files"].items()):
            v = [x["ns_op"] for x in f["samples"]]
            if len(v) >= 3:
                first.append((name, 100 * (v[0] / st.median(v[1:]) - 1)))
        yield s, ratios, ns, first


def main():
    print("=" * 72)
    print("A/A 逐輪比值（真值恆為 1.000，故任何偏離都是量測誤差）")
    for s, ratios, ns, first in aa_sessions():
        v = [x for _, x in ratios]
        print(f"\n[benchtime={s['benchtime']}] run {s['run_id']} · {s['cpu']}")
        print(f"  {s['note']}")
        print("  逐輪 B/A-1 (%): " + " ".join(f"{x:+.2f}" for x in v))
        print(f"  n={len(v)}  中位={st.median(v):+.2f}%  最小={min(v):+.2f}%  "
              f"最大={max(v):+.2f}%  全距={max(v)-min(v):.2f}pp  rSD={st.pstdev(v):.2f}pp")
        print(f"  |ratio-1|>5% 的輪數: {sum(1 for x in v if abs(x) > 5)}/{len(v)}")
        spread = 100 * (max(ns) - min(ns)) / st.mean(ns)
        print(f"  b.N 範圍: {min(ns)}..{max(ns)} (相對散佈 {spread:.1f}%)")
        big = [(n, e) for n, e in first if abs(e) > 5]
        print(f"  首樣本 vs 同檔其餘中位：|超額|>5% 的 invocation "
              f"{len(big)}/{len(first)}" + (f" ⇒ {big}" if big else ""))

    print("\n" + "=" * 72)
    print("本機 -benchtime=Nx sweep：邊際成本與彈性")
    rows = []
    for line in open(os.path.join(HERE, "local-benchtime-sweep.tsv"), encoding="utf-8"):
        if line.startswith("#") or line.startswith("bench\t") or not line.strip():
            continue
        b, var, n, ms = line.split("\t")
        rows.append((b, var, int(n), float(ms)))
    for key in sorted({(b, v) for b, v, _, _ in rows}):
        pts = sorted((n, ms) for b, v, n, ms in rows if (b, v) == key)
        print(f"\n{key[0]} [{key[1]}]")
        prev = None
        for n, ms in pts:
            if prev:
                marg = (n * ms - prev[0] * prev[1]) / (n - prev[0])
                print(f"  迭代 {prev[0]+1:>5}..{n:<5} 邊際 {marg:7.3f} ms/iter")
            prev = (n, ms)
        d = dict(pts)
        if 400 in d and 800 in d:
            e = math.log(d[800] / d[400]) / math.log(2)
            print(f"  彈性 dln(ns/op)/dln(N) (N=400->800) = {e:+.3f}"
                  f"  ⇒ 固定 benchtime 放大倍率 {1/(1-abs(e)):.2f}x")


if __name__ == "__main__":
    main()
