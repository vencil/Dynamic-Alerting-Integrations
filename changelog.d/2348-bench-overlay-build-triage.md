---
section: Changed
topic: ci
issues: [2348]
created: 2026-09-28T14:29:48+00:00
---
- **bench-workload-effect 的參考版本與夜跑解耦，W/R 與 M/W 不必等下一個 exporter tag（ci；[#2348](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2348)）**：新增 dispatch 輸入 `reference_sha`。留空時用夜跑釘死的參考；填入時必須是 main 已包含的 40 碼 SHA，否則 exit 1、不 checkout；summary 會標明不是夜跑參考、M/R 不能拿去對 `bench-paired.json`。PR self-test 改以「這支 PR 合上去的 main tip」當參考，另對釘死的參考做一次只出 notice 的編譯探測；self-test 恢復後 `bench_interleave.sh` 與 `pair_bench_ratio.py` 也會觸發它。overlay 編不過時依編譯器點名的檔案分流訊息，落在 `*bench_test.go` 就指向換一個較新的 `reference_sha`。夜跑參考的政策不變。
