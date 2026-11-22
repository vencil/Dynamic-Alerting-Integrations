---
section: Changed
topic: confd-family
issues: [4097]
created: 2026-11-22T13:22:00+00:00
---
- **`_expected_exporters` 的條目可以寫成 `種類@叢集`，缺席告警逐叢集判定（exporter、rule packs、tenant-api、portal、tools；[#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097)）**：同一個租戶跑在好幾個叢集時，只寫種類（`mariadb`）是任一個叢集有 exporter 回應就算存在，一個叢集的 exporter 消失不會告警。寫成 `_expected_exporters: [mariadb@east, mariadb@west, redis]` 後，`TenantExporterAbsent` 對沒有回應的那個宣告叢集告警（告警的 `da_cluster` 就是它），`TenantExporterUndeclared` 也逐叢集指名。叢集是該叢集 Prometheus 的 `da_cluster`；同一個種類只能擇一寫法，混寫或 `@` 後面不是叢集 ID 時整個值不讀。exporter 對這種條目送 `tenant_expected_exporter{tenant, pack, expected_cluster}`。tenant-api 與 portal 照寫的樣子顯示條目，以種類篩選時 `mariadb` 會找到 `mariadb@east`；policy 的 `one_of`／`equals` 比的是整個條目。⚠️ 限制：逐叢集的判定要在收了各叢集資料、序列帶 `da_cluster` 的中央 Prometheus 上才成立；各叢集各自評估告警規則的部署，`種類@叢集` 等同只寫種類，本地缺這一種時會對每個宣告的叢集各發一則，請只寫種類。沒有叢集清單：叢集 ID 打錯會表現為對那個 ID 的缺席告警。未做：`MassExporterOutage` 仍以（租戶、種類）計數，不分叢集——門檻與比例是全艦隊的，宣告經哪個叢集送到中央都只算一次（各來源的宣告只差 `da_cluster` 時），告警不帶 `da_cluster`；`CustomRecipeDiskInert`、`VersionAwareThresholdInert` 仍以整個租戶判定。
