---
section: Changed
topic: ci
issues: [1400]
created: 2026-11-21T23:30:18+00:00
---
- **nightly 映像掃描的 customer-delivered 追蹤 issue 能分辨「上游撤除」與「限流」（ci；[#1400](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1400)）**：report job 每晚加跑 `check_image_refs_resolve.py --scope delivered`，結果交給 `file_cve_report.sh`。registry 確認不存在的 ref 會寫進標題，且**只要仍不存在就每晚留言**（唯一的 level-based 例外：撤除會讓每條拉它的 pipeline 失敗，修法在我們手上）；拉不到但存在性檢查解析得到的，改寫為「多半是限流或暫時錯誤、不是撤除」，照舊只在覆蓋率下降那晚通知；存在性檢查本身判不出（registry 無回應、檢查失敗或沒跑）時明講「無法判定」，不當成存在也不當成撤除。其餘兩個 bucket 行為不變。
