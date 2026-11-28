# exporter_capture 測試夾具

`check_exporter_capture.py` 的邏輯測試用的 `/metrics` 抓取結果（`tests/lint/test_check_exporter_capture.py`）。

- 來源：postgres_exporter v0.21.1 的上游 release binary（`postgres_exporter-0.21.1.linux-amd64.tar.gz`，sha256 與上游 `sha256sums.txt` 相符）對本機 PostgreSQL 16.15 的一組 streaming replication（primary＋hot standby），以只持有 `pg_monitor` 的 `monitor` 使用者連線。
- `postgresql.<state>.prom`：帶 registry 宣告的旗標（`--collector.postmaster`）。
- `postgresql.<state>.no-flags.prom`：同一支 exporter 不帶任何旗標。
- 只留 pack 讀的 family，加上 `pg_replication_*` 與 `postgres_exporter_build_info`；其餘行刪掉了，留下的行沒有改。

這不是 registry 釘住的映像的輸出：映像只有 CI 的 `Exporter Capture (PostgreSQL)` job 會跑（`scripts/ops/exporter_capture_runtime.sh postgresql`）。這裡的檔案只用來在沒有 docker 的地方測檢查器本身，exporter 升版時不必跟著重抓，除非測試因此轉紅。
