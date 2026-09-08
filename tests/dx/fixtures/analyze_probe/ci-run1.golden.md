## Probe: write vs load latency (#1497 mechanism 1)

```
PROBEENV goos=linux goarch=amd64 numcpu=4 go=go1.26.7 iters=400 b.N=1
PROBEENV goos=linux goarch=amd64 numcpu=4 go=go1.26.7 iters=400 b.N=30
rounds parsed: 30 （已排除 1 個 b.N==1 的校準輪）
```

| quantity | value |
|---|---|
| round total, median | 3881.3 ms |
| round total, spread (max-min)/median | **10.7%** |
| write share of all time | **0.942%** |
| write_sum spread | 149.3% |
| load_sum spread | 9.4% |

最慢的一輪：round=10，比中位多 284.8 ms（+7.3%）。
其中寫入貢獻 +47.0 ms、載入貢獻 +237.7 ms ⇒ 超額有 **16.5%** 落在寫入路徑。

該輪的寫入尾端：p99=3300.1 µs、max=5651.6 µs；載入 p99=20.12 ms、max=21.06 ms。

### 這批 round-to-round 變異是什麼形狀

| 分量 | corr(round total, ·) | 自身 sd | 自身 sd ÷ round total sd |
|---|---|---|---|
| 水位 `p50 × iters` | +0.958 | 87.13 ms | 105.0% |
| 水位以上的質量 | -0.255 | 25.37 ms | 30.6% |
| `write_sum` | +0.642 | 9.38 ms | 11.3% |

⚠️ 相關係數說「哪一個跟著動」，最後一欄說「它最多能推動多少」。一個分量要當成因，兩欄都要成立。⛔ 這已有實測背書：形狀已知的合成資料上，純 episode 時「水位」與「水位以上的質量」的相關係數**都是約 +1.000**——第一欄分不出來——而 sd 佔比一個只有幾個百分點、一個接近全部。⇒ 兩欄一起讀；精確數字跑 `python3 -B docs/internal/audit-reports/bench-probe-2026-09/counterfactual.py`（TRK-374 / #1732）。

⛔ 最後一欄**不是** variance 分解，三列不會加總到 100%：三個分量彼此相關，>100% 表示它被另一個分量抵銷掉一部分（水位平移時水位與『水位以上的質量』反向）。

### 機制 1 要達到 #1497 的量級得做到什麼

單側擺 26.40% 在 3881 ms 的一輪上是 **1025 ms**。本次 30 輪裡，`write_sum` 高於中位最多的一輪是 **47.0 ms**（差 **22×**）。
換算到每次迭代：中位輪的寫入平均 85.3 µs，要達標得在全部 400 次迭代上平均 2.56 ms，即整個寫入路徑成本的 **30×**、而且是**持續**不是尖峰。

> ⛔ 判讀限制：本 job 只跑一台 runner、一個 job，跨輪散佈與 #1497 觀察到的**跨 dispatch** 散佈不是同一個量。若上面的 spread 沒有重現 #1497 的量級，那是**這次沒抓到**，不是「沒有」。
