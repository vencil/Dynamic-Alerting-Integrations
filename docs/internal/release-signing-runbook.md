---
title: "Release Signing Runbook"
tags: [release, security, supply-chain, internal]
audience: [maintainers]
version: v2.7.0
verified-at-version: v2.8.0
lang: zh
---

# Release Signing Runbook

> **內部 maintainer-facing runbook** — 解釋 `tools/v*` Migration Toolkit
> release 的簽章層級、現況、以及客戶要求新簽章方式時的啟動路徑。
>
> **關連文件**：
> - 客戶端 verification 流程：[`docs/migration-toolkit-installation.md` §Signature Verification](../migration-toolkit-installation.md#signature-verification)
> - Release CI：`.github/workflows/release.yaml` 的 `release-da-tools` job（從 GitHub 端開啟）
> - Customer activation issue template：`.github/ISSUE_TEMPLATE/signing-request.md`（從 GitHub 端開啟）

## 為什麼需要分層

每加一條簽章路徑（cosign / GPG / Authenticode）= 永久 contract + 維護
成本 + 失敗模式。一次全做不只成本相加而是相乘（每次 release 要驗 4 套
都對，任一爆 = release blocked）。同時無法預先知道客戶 security team 要
哪一種驗章方式。所以設計成 **三層分階**：

- **Layer 1 — pre-built（v2.8.0 起預設啟動）**：覆蓋 70-80% 雲原生客戶的
  最低門檻 supply-chain hygiene。出貨時就有，客戶端立即可驗。
- **Layer 2 — customer-driven activation**：保留快速啟用路徑但 default
  off。客戶 onboarding security review 提需求 → maintainer 24-48h 內
  enable 對應 path。
- **Layer 3 — never pre-built**：超出雲原生 baseline 的 enterprise
  compliance（HSM / FIPS-140 / SLSA Level 3）。等客戶 RFP 才做。

---

## Layer 1（active）：cosign keyless + SBOM

**目前已啟動於每個 `tools/v*` release**。

### 啟動位置

`.github/workflows/release.yaml::release-da-tools` job：

| 步驟 | 用途 |
|---|---|
| `Install cosign` | sigstore/cosign-installer@v3.7.0 pinning cosign v2.4.1 |
| `Install syft` | anchore/sbom-action/download-syft@v0.17.7 pinning syft v1.18.1 |
| `Sign Docker image` | `cosign sign` by digest (image@sha256:...)，不簽 tag (mutable) |
| `Generate SBOM for Docker image` | syft 產 SPDX + CycloneDX 兩份格式 |
| `Sign SBOM files` | cosign sign-blob 對 SBOM 也加簽 (tampered SBOM defeats supply chain) |
| `Sign archives + SHA256SUMS` | 對 18 個 binary archive + SHA256SUMS index 各產 .sig + .cert |
| `Sign air-gapped image tar` | `da-tools-image-v*.tar.gz` 也加簽 |

### Permissions 必填

`release-da-tools` job 必須有 `id-token: write` permission（cosign 用
GitHub OIDC token 換 Fulcio 臨時 cert）。已加在 release.yaml L124-130。

### 客戶端驗證

完整客戶 flow 見
[`docs/migration-toolkit-installation.md` §Signature Verification](../migration-toolkit-installation.md#signature-verification)。

我們也提供 `scripts/tools/dx/verify_release.sh` 友善 wrapper，做完整
sha256 + cosign chain。

### Air-gapped 注意事項

cosign keyless 預設依賴 Sigstore TUF root + Rekor transparency log
查驗。**air-gapped 客戶**：
- 預先 sync TUF root：`cosign initialize --mirror <local-mirror>`
- Or 使用 offline 驗證 mode：`COSIGN_EXPERIMENTAL=1 cosign verify-blob`
  with `--insecure-ignore-tlog` (失去 transparency 但保留簽章驗證)
- 完整 air-gapped runbook：等 customer 來時 case-by-case，目前 toolkit
  layer 1 對 air-gapped 是「可驗但需要 TUF mirror 環境準備」

### 失敗模式 + 修復

| 症狀 | 可能原因 | 修復 |
|---|---|---|
| cosign install step 失敗 | sigstore 升上游 break (rare ~年 1-2 次) | pin 至已 verified 版本（升 cosign-installer action 與 cosign release tag）|
| `sign-blob` exit non-zero | OIDC token issuance 失敗（permissions 缺 id-token: write）| 確認 release.yaml job permissions block 含 id-token: write |
| Customer 驗證 fail with "certificate identity not in known list" | tag 不在 main branch / 或來自 fork | 客戶確認下載自正確 repo + tag；驗證 script 印出的 expected identity |
| Rekor service down at customer-side verify time | sigstore upstream incident | 客戶可暫時 `--insecure-ignore-tlog`（失 transparency 但簽章還能驗）|

---

## Layer 2（customer-driven activation）

預埋接口，**default off**。客戶 security team 要求才啟用。

### Activation flow

1. Customer 開 `signing-request` issue（template at `.github/ISSUE_TEMPLATE/signing-request.md` — 從 GitHub 端開啟），指定 mechanism + 用途
2. Maintainer 24-48h 內回應，確認 scope（單一 release / 持續每 release）
3. 啟動對應 sub-section（看下面 GPG / Authenticode 子節）
4. Customer 驗 sample release（new release with signing 啟動），確認流程通
5. 如果是「持續每 release」需求，scope 寫入 release.yaml + 加進 release checklist

### 2a. GPG signing

**何時用**：客戶 air-gapped + 不接受 sigstore transparency log 模式 / 政策
明文要求 GPG 為 baseline。

**啟動代價**：
- 創 long-term GPG key（建議 ed25519 + RSA 4096 backup, 4 year expiry）
- Master key 離線保管於 hardware token (YubiKey / Nitrokey)
- Subkey 用於 CI 簽署，存 `secrets.GPG_SIGNING_KEY` (passphrase-encrypted ASCII armor) + `secrets.GPG_SIGNING_KEY_PASSPHRASE`
- 公鑰發布到 keys.openpgp.org + 寫入 README + 寫進 migration-toolkit-installation 文件
- Maintainer 換手 = key rotation cycle (subkey revoke → new subkey → re-sign 已發 release? scope 看 customer)

**新增 release.yaml step**（參考 stub，啟動時 uncomment + adjust）：

```yaml
- name: Import GPG signing key
  if: false  # ← change to: env.ENABLE_GPG_SIGNING == 'true' or always()
  uses: crazy-max/ghaction-import-gpg@v6
  with:
    gpg_private_key: ${{ secrets.GPG_SIGNING_KEY }}
    passphrase: ${{ secrets.GPG_SIGNING_KEY_PASSPHRASE }}

- name: GPG sign release archives
  if: false  # ← gate same as above
  env:
    GPG_KEY_ID: ${{ secrets.GPG_KEY_ID }}
  run: |
    set -euo pipefail
    cd "$RUNNER_TEMP/da-guard-bin"
    for f in *.tar.gz *.zip SHA256SUMS; do
      gpg --batch --yes --pinentry-mode loopback \
          --local-user "$GPG_KEY_ID" \
          --armor --detach-sign --output "${f}.asc" "$f"
    done
```

**客戶驗證 SOP**：見 `migration-toolkit-installation.md` §GPG Verification（待補；activation 時加）。

### 2b. Windows Authenticode

**何時用**：客戶要求 native Windows binary 跑 (而非 container)，且
Windows SmartScreen 警告為 onboarding blocker。

**啟動代價**：
- 採購 Code Signing Certificate（DigiCert / Sectigo / SSL.com，~$300/年
  普通 / ~$500-$3000/年 EV）
- EV cert 需要 LLC 或法人實體驗證（耗時 1-2 週、文件費用）
- 證書 key 存 HSM / USB token（CA/Browser Forum 2023+ baseline 強制）
- CI 整合：windows runner + signtool.exe + timestamp server (DigiCert
  http://timestamp.digicert.com 或對應 CA)

**啟動方式**（高層）：
- Add windows runner job 給 release.yaml
- Use `signtool sign /n "<subject>" /tr <timestamp-url> /td sha256 /fd sha256 da-parser-windows-amd64.exe`
- Re-zip 簽完的 binary

**Cost-benefit threshold**：典型「等到客戶下載量 + 反饋累積到證明 EV
cert ROI 比簽純 cosign 高」才啟動。

### 2c. 雙簽（cosign + GPG 並行）

**何時用**：客戶混合 fleet — 部分 cloud-native CI 用 cosign / 部分
air-gapped 用 GPG。

**啟動代價**：Layer 1 已活 + Layer 2a 啟動 = 自動雙簽。額外維護成本主
要在 docs（要寫雙路徑驗證）+ release notes（兩個 verification block）。

---

## Layer 3（never pre-built）

超出 Layer 2 範圍的 enterprise compliance：

| 需求 | 為何 Layer 3 |
|---|---|
| HSM-backed signing keys | 需要實體 HSM (YubiHSM / Thales 等)，硬體採購 + key ceremony 流程 |
| FIPS 140-2 hardware token | 同上，且 cert 必須由 FIPS-validated CA 發 |
| SLSA Level 2 / Level 3 | 需要 isolated build infra (GitHub-hosted runner 不夠)，build provenance attestation chain |
| Reproducible builds | Go 已可大致 reproducible，但 Docker layer 完全 reproducible 需要 nix 或 bazel 重構 |
| In-toto attestation chain | 需要 attestation framework (witness / spire) integration |
| 21 CFR Part 11 / GxP | FDA 規範，非技術問題 — 需要 SOP + audit log + 簽章人工身分驗證 |
| 中國 / 俄羅斯 LOCAL CA chain | 政策驅動，需要對應地區 CA root + 繁體政府要求的 timestamp server |

**處置**：客戶 RFP 來時 case-by-case 評估，可能需要 maintainer + 客戶
security architect 共同設計 isolated path（不歸 mainline release.yaml）。

---

## Maintainer onboarding checklist

新 maintainer 接手 release 工作時讀過：

- [ ] 確認自己 GitHub account 有 repo `Maintainers` 或更高權限（push tag → release.yaml fire）
- [ ] 跑一次 dry-run release 觀察 Layer 1 全流程（cosign / SBOM / upload）
- [ ] 讀過本 runbook §Layer 1 失敗模式表
- [ ] 知道 `signing-request` issue template 在哪
- [ ] 確認 `migration-toolkit-installation.md` §Signature Verification 範例命令仍對應 release.yaml 實際輸出（每次 release 後抽查）

---

## 變更歷史

| 版本 | 變更 | 對應 PR |
|---|---|---|
| v2.8.0 | 文件建立；Layer 1 (cosign keyless + SBOM SPDX/CycloneDX) 活、Layer 2/3 deferred 設計 | C-11 PR-3 |
