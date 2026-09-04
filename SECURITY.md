# Security Policy

## 支援的版本

本專案採六線版號（`v*` / `exporter/v*` / `tools/v*` / `portal/v*` / `recipe-preview/v*` / `tenant-api/v*`）。

**每條線只有最新的已發布 tag 受安全支援**，更早的版本不回補安全修補。各線目前的最新版本見
[Releases](https://github.com/vencil/Dynamic-Alerting-Integrations/releases)——本檔刻意不複製版號，
以免它腐爛成一份與實際發布不符的清單。

⚠️ 判準一律以 Releases 為準：**某條線在 Releases 上沒有任何版本，就代表它目前沒有受支援的已發布版本**。
（`recipe-preview/v*` 是隨平台 release 重打的「同步升」線，不走獨立 cadence。）

## 回報漏洞

⛔ **請不要用公開 issue、PR 或 discussion 回報安全問題。** 本 repo 是公開的，開出 issue 的當下即等同揭露。

請走 GitHub 的 private vulnerability reporting（本 repo 已啟用）：

👉 **[Security → Advisories → Report a vulnerability](https://github.com/vencil/Dynamic-Alerting-Integrations/security/advisories/new)**

這條路徑會開一個**只有你與維護者看得到**的私有回報。它先進入 `Triage` 狀態；維護者確認後按
**Accept and open as draft**，才會成為 draft advisory。⇒ 提交當下不會自動產生 advisory，
也不會有任何內容因此變成公開的。

回報時若能包含以下資訊，會大幅縮短確認時間：

- 受影響的元件與版本或 commit。元件即 `components/` 下的五個（`threshold-exporter` / `tenant-api` /
  `da-tools` / `da-portal` / `recipe-preview`），或 helm chart / k8s manifests
- 重現步驟，或最小 PoC
- 你**實際觀察到**的影響（而非推測的影響）；若某一段是推測而未實測，請直接標明——這對評分比誇大有用得多

## 你可以預期什麼

| 階段 | 承諾 |
|---|---|
| 首次回應 | **7 天內** |
| 後續進度 | 在同一則 advisory 的討論串內更新 |

## 揭露流程

1. 私下確認與評估（含 CVSS 向量與分數）。
2. **修補先進 release tag，advisory 才發布**——在修補可取得之前不公開細節。
3. 發布 advisory 時回填 `Patched versions`。
4. 回報者會列入 advisory 的 credits，除非你明確表示不要。

---

## English

**Do not report security issues through public issues, pull requests, or discussions** — this repository is
public, so opening one is itself a disclosure.

Please use GitHub's private vulnerability reporting, which is enabled on this repository:

👉 **[Security → Advisories → Report a vulnerability](https://github.com/vencil/Dynamic-Alerting-Integrations/security/advisories/new)**

This opens a report visible only to you and the maintainer. It starts in `Triage`; it becomes a draft
advisory only after the maintainer explicitly accepts it. Submitting a report never publishes anything.

Please include the affected component and version/commit, reproduction steps or a minimal PoC, and the impact
you actually observed. If part of your report is inferred rather than measured, say so explicitly.

**Supported versions:** only the latest released tag on each of the six release lines is supported; see
[Releases](https://github.com/vencil/Dynamic-Alerting-Integrations/releases). A line with no entry in
Releases has no supported released version.

**First response:** within **7 days**, with progress updates in the same advisory thread.

**Disclosure:** advisories are published only after a fix ships in a release tag; `Patched versions` is filled
in at publication. Reporters are credited unless they ask not to be.
