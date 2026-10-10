---
title: "ADR-002: OCI Registry 替代 ChartMuseum"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
lang: zh
id: ADR-002
tracking_kind: adr
status: accepted
domain: helm
created_at: 2026-03-13
updated_at: 2026-10-10
---
# ADR-002: OCI Registry 替代 ChartMuseum

> **Language / 語言：** **中文 (Current)** | [English](./002-oci-registry-over-chartmuseum.en.md)

**決策摘要**：Helm chart 與容器映像放在同一個容器映像倉庫（ghcr.io），chart 以 OCI 製品的形式發佈，不另外架設 ChartMuseum。

## 狀態

✅ **Accepted**（v1.12.0）

## 名詞

- **OCI 製品（OCI artifact）**：符合 Open Container Initiative 格式、可以存進容器映像倉庫的檔案。容器映像是其中一種，Helm chart 也可以用這個格式存放。
- **ChartMuseum**：專門存放 Helm chart 的獨立服務，客戶端以 `helm repo add` 接上它。
- **ghcr.io**：GitHub 提供的容器映像倉庫（GitHub Container Registry）。

## 背景

平台要發佈兩類東西：

1. **Helm chart**：threshold-exporter、da-portal、tenant-api 的部署設定。
2. **容器映像**：threshold-exporter、da-tools、da-portal、tenant-api。

常見做法是映像放容器映像倉庫（例如 ghcr.io），chart 另外放在 ChartMuseum 這類 chart 倉庫，於是有兩套要維運的基礎設施。

## 決策

**統一使用 OCI 容器映像倉庫（ghcr.io），同時存放 Helm chart 與容器映像，不依賴 ChartMuseum。**

Helm 3.8 起原生支援 OCI：chart 可以直接推進容器映像倉庫，以 OCI 製品的方式做版本管理與存取控制。

### 範例

發佈端（release workflow）把打包好的 chart 推到 `charts/` 底下：

```bash
helm push .build/threshold-exporter-2.9.0.tgz oci://ghcr.io/vencil/charts
```

同一個倉庫裡，映像與 chart 並排：

| 製品 | 位置 |
|:--|:--|
| 容器映像 | `ghcr.io/vencil/threshold-exporter:v2.9.0` |
| Helm chart | `oci://ghcr.io/vencil/charts/threshold-exporter`，版本 `2.9.0` |

安裝端不需要 `helm repo add`，直接指定 OCI 位址：

```bash
helm install threshold-exporter \
  oci://ghcr.io/vencil/charts/threshold-exporter --version 2.9.0 \
  -n monitoring --create-namespace -f values-override.yaml
```

倉庫中這個 chart 的 manifest，設定層的媒體類型是 `application/vnd.cncf.helm.config.v1+json`，也就是以 Helm chart 的身分存放的 OCI 製品。

## 後果與已知限制

**得到的**

- 只維運一個倉庫：不必另外顧 ChartMuseum 的實例、備份與高可用設定，也省下這套服務的基礎設施費用。
- chart 本身的內容不需要為此修改，改的只有發佈與安裝的方式。

**要承擔的**

- 安裝端需要 Helm 3.8 以上。企業內仍有舊版 Helm 時，要安排升級。
- 部分 Helm 外掛（例如 helm-diff）需要另外確認與 OCI chart 的相容性。

## 考慮過的替代方案

### 保留 ChartMuseum，與 ghcr.io 雙軌並行（不採用）

相容所有舊版 Helm，但要維護兩套基礎設施，維運複雜度加倍。

### 改用 Artifactory 或 Nexus（不採用）

企業級功能完整，但需要自建或付費，功能與 ghcr.io 重疊，團隊也要另外學一套工具。

## 相關

- [Helm 官方文件：Registries（OCI 支援）](https://helm.sh/docs/topics/registries/)
- [threshold-exporter README §6 部署](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/README.md#6-部署) — 以 OCI chart 安裝的完整指令
