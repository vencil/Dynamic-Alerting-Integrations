---
section: Fixed
topic: tenant-api
issues: [3228]
created: 2026-10-21T16:53:59+00:00
---
- **tenant-api chart：不覆寫 image 時部署的是缺 flag 的舊 binary（[#3228](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3228)）**：`Chart.yaml` 的 `appVersion` 停在 2.7.0，預設 image 因此是 `tenant-api:v2.7.0`，而 chart 2.9.24 的 template 會傳 `--generator-socket` 等那版 binary 沒有的 flag。⚠️ 行為改變：tenant-api 改成單一版號，`appVersion` == `version` == `tenant-api/v*` tag（本版 2.9.24），預設 image 就是與 chart 同一次 release 建出的 `tenant-api:v<version>`。`bump_docs.py --tenant-api <ver>` 兩個一起改；release job 與 `tests/helm/test_image_tag_derivation.py` 在不相等時 fail。產生器容器的 da-tools 映像走 `tools/v*` 線，不受影響。在 `tenant-api/v<version>` 發布之前，main 上 chart 的預設 image 尚不存在（與 threshold-exporter 等線相同）。
