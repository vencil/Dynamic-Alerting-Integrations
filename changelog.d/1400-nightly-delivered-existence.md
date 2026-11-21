---
section: Changed
topic: ci
issues: [1400]
created: 2026-11-21T23:30:18+00:00
---
- **nightly 映像掃描的 customer-delivered 追蹤 issue 能分辨「拉不到的 ref」與「限流」（ci；[#1400](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1400)）**：report job 每晚加跑 `check_image_refs_resolve.py --scope delivered`，結果交給 `file_cve_report.sh`。registry 匿名解析不到的 ref（撤除、尚未發布、改名或 private，附 registry 的原因）計入標題的「N unresolvable」，且**只要仍解析不到就每晚留言**；修法依歸屬分：自家 `ghcr.io/vencil/` 是推 `tools/v<VERSION>` tag（release 空窗期也照留），第三方是 bump pin 並通知客戶。拉不到但解析得到的改寫為「多半是限流或暫時錯誤」，照舊只在覆蓋率下降那晚通知；存在性檢查本身判不出時明講「無法判定」。其餘兩個 bucket 的判讀與措辭不變；唯一變化是 aggregate 步驟改為 `!cancelled()`，download-artifact 失敗時仍會執行，缺的 bucket 以全數 missing 告警，而不是整個跳過。
