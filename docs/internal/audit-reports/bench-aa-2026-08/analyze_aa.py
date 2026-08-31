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
    pooled = []
    for s, ratios, ns, first in aa_sessions():
        pooled += [(x, s["benchtime"], s["run_id"], r) for r, x in ratios]
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

    # ⛔ 這一段刻意存在：散文引用的是「**絕對**偏離最大」，而逐場那幾行只給
    #    各場的 min/max。初版散文因此把 −3.09%（600x 第 4 輪）漏掉、寫成
    #    「最大偏離 +2.25%」，把噪音上界低報 37% —— 而且是往「儀器看起來更準」
    #    的方向錯。把散文要用的那個數字直接算出來，才不會再靠人去掃表格。
    pooled.sort(key=lambda t: -abs(t[0]))
    worst = pooled[0]
    print(f"\n[全部場次合計] n={len(pooled)} 輪")
    print(f"  |ratio-1| 最大 = {abs(worst[0]):.2f}%  （{worst[0]:+.2f}%，"
          f"benchtime={worst[1]}、run {worst[2]}、第 {worst[3]} 輪）")
    print(f"  正向最大 = {max(x for x, *_ in pooled):+.2f}%   "
          f"負向最大 = {min(x for x, *_ in pooled):+.2f}%")
    print(f"  |ratio-1|>5% 共 {sum(1 for x, *_ in pooled if abs(x) > 5)}/{len(pooled)}")

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
        # ⛔ `observed-at-3s` 不是 sweep：那四列是同一個設定下的重複觀測，
        #    橫軸不是「累積到第 N 次迭代」，對它做相鄰差分會得到無意義的數
        #    （實測會印出 -102 ms/iter 這種值）。它只用來看 b.N 的散佈。
        prev = None if key[1] == "observed-at-3s" else None
        if key[1] != "observed-at-3s":
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
        if key[1] == "observed-at-3s":
            v = [n for n, _ in pts]
            print(f"  ⇒ benchtime=3s 下 Go 自選的 b.N: {v}"
                  f"  相對散佈 {100*(max(v)-min(v))/(sum(v)/len(v)):.1f}%"
                  f"  （連續，非離散分層）")


def multibench_control():
    """預設四支的 A/A 對照。⛔ 與 aa-sessions.json 不同的 benchmark 集合，不池化。"""
    path = os.path.join(HERE, "aa-multibench-control.tsv")
    if not os.path.exists(HERE) or not os.path.exists(path):
        return
    per = {}
    for line in open(path, encoding="utf-8"):
        if line.startswith("#") or line.startswith("round\t") or not line.strip():
            continue
        r, side, bench, n, ns = line.rstrip("\n").split("\t")
        per.setdefault((bench, int(r)), {}).setdefault(side, []).append(float(ns) / 1e6)
    print("\n" + "=" * 72)
    print("預設四支的 A/A 對照（不同 benchmark 集合，⛔ 不與上面池化）")
    allv = []
    for bench in sorted({b for b, _ in per}):
        v = []
        for r in sorted(x for bb, x in per if bb == bench):
            s = per[(bench, r)]
            if "A" in s and "B" in s:
                v.append(100 * (st.median(s["B"]) / st.median(s["A"]) - 1))
        if not v:
            continue
        allv += v
        print(f"  {bench:<45} n={len(v)}  rSD {st.pstdev(v):.2f}pp  "
              f"|偏離|最大 {max(abs(x) for x in v):.2f}%")
    if allv:
        print(f"  四支合計 n={len(allv)}：rSD {st.pstdev(allv):.2f}pp、"
              f"|偏離|最大 {max(abs(x) for x in allv):.2f}%、"
              f"|ratio-1|>5% 共 {sum(1 for x in allv if abs(x) > 5)}/{len(allv)}")


if __name__ == "__main__":
    main()
    multibench_control()
